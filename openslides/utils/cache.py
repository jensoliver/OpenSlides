import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from asgiref.sync import async_to_sync
from django.conf import settings

from .cache_providers import (
    Cachable,
    ElementCacheProvider,
    MemmoryCacheProvider,
    RedisCacheProvider,
    get_all_cachables,
)
from .redis import use_redis
from .utils import get_element_id, split_element_id


logger = logging.getLogger(__name__)


class ElementCache:
    """
    Cache for the elements.

    Saves the full_data and if enabled the restricted data.

    There is one redis Hash (simular to python dict) for the full_data and one
    Hash for every user.

    The key of the Hashes is COLLECTIONSTRING:ID where COLLECTIONSTRING is the
    collection_string of a collection and id the id of an element.

    All elements have to be in the cache. If one element is missing, the cache
    is invalid, but this can not be detected. When a plugin with a new
    collection is added to OpenSlides, then the cache has to be rebuild manualy.

    There is an sorted set in redis with the change id as score. The values are
    COLLETIONSTRING:ID for the elements that have been changed with that change
    id. With this key it is possible, to get all elements as full_data or as
    restricted_data that are newer then a specific change id.

    All method of this class are async. You either have to call them with
    await in an async environment or use asgiref.sync.async_to_sync().
    """

    def __init__(
        self,
        use_restricted_data_cache: bool = False,
        cache_provider_class: Type[ElementCacheProvider] = RedisCacheProvider,
        cachable_provider: Callable[[], List[Cachable]] = get_all_cachables,
        start_time: int = None,
    ) -> None:
        """
        Initializes the cache.

        When restricted_data_cache is false, no restricted data is saved.
        """
        self.use_restricted_data_cache = use_restricted_data_cache
        self.cache_provider = cache_provider_class()
        self.cachable_provider = cachable_provider
        self._cachables: Optional[Dict[str, Cachable]] = None

        # Start time is used as first change_id if there is non in redis
        if start_time is None:
            # Use the miliseconds (rounted) since the 2016-02-29.
            start_time = (
                int((datetime.utcnow() - datetime(2016, 2, 29)).total_seconds()) * 1000
            )
        self.start_time = start_time

        # Tells if self.ensure_cache was called.
        self.ensured = False

    @property
    def cachables(self) -> Dict[str, Cachable]:
        """
        Returns all Cachables as a dict where the key is the collection_string of the cachable.
        """
        # This method is neccessary to lazy load the cachables
        if self._cachables is None:
            self._cachables = {
                cachable.get_collection_string(): cachable
                for cachable in self.cachable_provider()
            }
        return self._cachables

    def ensure_cache(self, reset: bool = False) -> None:
        """
        Makes sure that the cache exist.

        Builds the cache if not. If reset is True, it will be reset in any case.

        This method is sync, so it can be run when OpenSlides starts.
        """
        cache_exists = async_to_sync(self.cache_provider.data_exists)()

        if reset or not cache_exists:
            lock_name = "ensure_cache"
            # Set a lock so only one process builds the cache
            if async_to_sync(self.cache_provider.set_lock)(lock_name):
                logger.info("Building up the cache data...")
                try:
                    mapping = {}
                    for collection_string, cachable in self.cachables.items():
                        for element in cachable.get_elements():
                            mapping.update(
                                {
                                    get_element_id(
                                        collection_string, element["id"]
                                    ): json.dumps(element)
                                }
                            )
                    logger.info("Done building the cache data.")
                    logger.info("Saving cache data into the cache...")
                    async_to_sync(self.cache_provider.reset_full_cache)(mapping)
                    logger.info("Done saving the cache data.")
                finally:
                    async_to_sync(self.cache_provider.del_lock)(lock_name)
            else:
                logger.info("Wait for another process to build up the cache...")
                while async_to_sync(self.cache_provider.get_lock)(lock_name):
                    sleep(0.01)
                logger.info("Cache is ready (built by another process).")

        self.ensured = True

    async def change_elements(
        self, elements: Dict[str, Optional[Dict[str, Any]]]
    ) -> int:
        """
        Changes elements in the cache.

        elements is a list of the changed elements as dict. When the value is None,
        it is interpreded as deleted. The key has to be an element_id.

        Returns the new generated change_id.
        """
        if not self.ensured:
            raise RuntimeError(
                "Call element_cache.ensure_cache before changing elements."
            )

        deleted_elements = []
        changed_elements = []
        for element_id, data in elements.items():
            if data:
                # The arguments for redis.hset is pairs of key value
                changed_elements.append(element_id)
                changed_elements.append(json.dumps(data))
            else:
                deleted_elements.append(element_id)

        if changed_elements:
            await self.cache_provider.add_elements(changed_elements)
        if deleted_elements:
            await self.cache_provider.del_elements(deleted_elements)

        return await self.cache_provider.add_changed_elements(
            self.start_time + 1, elements.keys()
        )

    async def get_all_full_data(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Returns all full_data.

        The returned value is a dict where the key is the collection_string and
        the value is a list of data.
        """
        all_data = await self.get_all_full_data_ordered()
        out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for collection_string, collection_data in all_data.items():
            for data in collection_data.values():
                out[collection_string].append(data)
        return dict(out)

    async def get_all_full_data_ordered(self) -> Dict[str, Dict[int, Dict[str, Any]]]:
        """
        Like get_all_full_data but orders the element of one collection by there
        id.
        """
        out: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)
        full_data = await self.cache_provider.get_all_data()
        for element_id, data in full_data.items():
            collection_string, id = split_element_id(element_id)
            out[collection_string][id] = json.loads(data.decode())
        return dict(out)

    async def get_full_data(
        self, change_id: int = 0, max_change_id: int = -1
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        """
        Returns all full_data since change_id until max_change_id (including).
        max_change_id -1 means the highest change_id.

        Returns two values inside a tuple. The first value is a dict where the
        key is the collection_string and the value is a list of data. The second
        is a list of element_ids with deleted elements.

        Only returns elements with the change_id or newer. When change_id is 0,
        all elements are returned.

        Raises a RuntimeError when the lowest change_id in redis is higher then
        the requested change_id. In this case the method has to be rerun with
        change_id=0. This is importend because there could be deleted elements
        that the cache does not know about.
        """
        if change_id == 0:
            return (await self.get_all_full_data(), [])

        # This raises a Runtime Exception, if there is no change_id
        lowest_change_id = await self.get_lowest_change_id()

        if change_id < lowest_change_id:
            # When change_id is lower then the lowest change_id in redis, we can
            # not inform the user about deleted elements.
            raise RuntimeError(
                f"change_id {change_id} is lower then the lowest change_id in redis {lowest_change_id}. "
                "Catch this exception and rerun the method with change_id=0."
            )

        raw_changed_elements, deleted_elements = await self.cache_provider.get_data_since(
            change_id, max_change_id=max_change_id
        )
        return (
            {
                collection_string: [json.loads(value.decode()) for value in value_list]
                for collection_string, value_list in raw_changed_elements.items()
            },
            deleted_elements,
        )

    async def get_collection_full_data(
        self, collection_string: str
    ) -> Dict[int, Dict[str, Any]]:
        full_data = await self.cache_provider.get_collection_data(collection_string)
        out = {}
        for element_id, data in full_data.items():
            returned_collection_string, id = split_element_id(element_id)
            if returned_collection_string == collection_string:
                out[id] = json.loads(data.decode())
        return out

    async def get_element_full_data(
        self, collection_string: str, id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Returns one element as full data.

        Returns None if the element does not exist.
        """
        element = await self.cache_provider.get_element(
            get_element_id(collection_string, id)
        )

        if element is None:
            return None
        return json.loads(element.decode())

    async def exists_restricted_data(self, user_id: int) -> bool:
        """
        Returns True, if the restricted_data exists for the user.
        """
        if not self.use_restricted_data_cache:
            return False

        return await self.cache_provider.data_exists(user_id)

    async def del_user(self, user_id: int) -> None:
        """
        Removes one user from the resticted_data_cache.
        """
        await self.cache_provider.del_restricted_data(user_id)

    async def update_restricted_data(self, user_id: int) -> None:
        """
        Updates the restricted data for an user from the full_data_cache.
        """
        # TODO: When elements are changed at the same time then this method run
        #       this could make the cache invalid.
        #       This could be fixed when get_full_data would be used with a
        #       max change_id.
        if not self.use_restricted_data_cache:
            # If the restricted_data_cache is not used, there is nothing to do
            return

        if not self.ensured:
            raise RuntimeError(
                "Call element_cache.ensure_cache before updating restricted data."
            )

        # Try to write a special key.
        # If this succeeds, there is noone else currently updating the cache.
        # TODO: Make a timeout. Else this could block forever
        lock_name = f"restricted_data_{user_id}"
        if await self.cache_provider.set_lock(lock_name):
            # Get change_id for this user
            value = await self.cache_provider.get_change_id_user(user_id)
            # If the change id is not in the cache yet, use -1 to get all data since 0
            user_change_id = int(value) if value else -1
            change_id = await self.get_current_change_id()
            if change_id > user_change_id:
                try:
                    full_data_elements, deleted_elements = await self.get_full_data(
                        user_change_id + 1
                    )
                except RuntimeError:
                    # The user_change_id is lower then the lowest change_id in the cache.
                    # The whole restricted_data for that user has to be recreated.
                    full_data_elements = await self.get_all_full_data()
                    deleted_elements = []
                    await self.cache_provider.del_restricted_data(user_id)

                mapping = {}
                for collection_string, full_data in full_data_elements.items():
                    restricter = self.cachables[collection_string].restrict_elements
                    restricted_elements = await restricter(user_id, full_data)

                    # find all elements the user can not see at all
                    full_data_ids = set(element["id"] for element in full_data)
                    restricted_data_ids = set(
                        element["id"] for element in restricted_elements
                    )
                    for item_id in full_data_ids - restricted_data_ids:
                        deleted_elements.append(
                            get_element_id(collection_string, item_id)
                        )

                    for element in restricted_elements:
                        # The user can see the element
                        mapping.update(
                            {
                                get_element_id(
                                    collection_string, element["id"]
                                ): json.dumps(element)
                            }
                        )
                mapping["_config:change_id"] = str(change_id)
                await self.cache_provider.update_restricted_data(user_id, mapping)
                # Remove deleted elements
                if deleted_elements:
                    await self.cache_provider.del_elements(deleted_elements, user_id)
            # Unset the lock
            await self.cache_provider.del_lock(lock_name)
        else:
            # Wait until the update if finshed
            while await self.cache_provider.get_lock(lock_name):
                await asyncio.sleep(0.01)

    async def get_all_restricted_data(
        self, user_id: int
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Like get_all_full_data but with restricted_data for an user.
        """
        if not self.use_restricted_data_cache:
            all_restricted_data = {}
            for collection_string, full_data in (
                await self.get_all_full_data()
            ).items():
                restricter = self.cachables[collection_string].restrict_elements
                elements = await restricter(user_id, full_data)
                all_restricted_data[collection_string] = elements
            return all_restricted_data

        await self.update_restricted_data(user_id)

        out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        restricted_data = await self.cache_provider.get_all_data(user_id)
        for element_id, data in restricted_data.items():
            if element_id.decode().startswith("_config"):
                continue
            collection_string, __ = split_element_id(element_id)
            out[collection_string].append(json.loads(data.decode()))
        return dict(out)

    async def get_restricted_data(
        self, user_id: int, change_id: int = 0, max_change_id: int = -1
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        """
        Like get_full_data but with restricted_data for an user.
        """
        if change_id == 0:
            # Return all data
            return (await self.get_all_restricted_data(user_id), [])

        if not self.use_restricted_data_cache:
            changed_elements, deleted_elements = await self.get_full_data(
                change_id, max_change_id
            )
            restricted_data = {}
            for collection_string, full_data in changed_elements.items():
                restricter = self.cachables[collection_string].restrict_elements
                elements = await restricter(user_id, full_data)

                # Add removed objects (through restricter) to deleted elements.
                full_data_ids = set([data["id"] for data in full_data])
                restricted_data_ids = set([data["id"] for data in elements])
                for id in full_data_ids - restricted_data_ids:
                    deleted_elements.append(get_element_id(collection_string, id))

                if elements:
                    restricted_data[collection_string] = elements
            return restricted_data, deleted_elements

        lowest_change_id = await self.get_lowest_change_id()
        if change_id < lowest_change_id:
            # When change_id is lower then the lowest change_id in redis, we can
            # not inform the user about deleted elements.
            raise RuntimeError(
                f"change_id {change_id} is lower then the lowest change_id in redis {lowest_change_id}. "
                "Catch this exception and rerun the method with change_id=0."
            )

        # If another coroutine or another daphne server also updates the restricted
        # data, this waits until it is done.
        await self.update_restricted_data(user_id)

        raw_changed_elements, deleted_elements = await self.cache_provider.get_data_since(
            change_id, user_id, max_change_id
        )
        return (
            {
                collection_string: [json.loads(value.decode()) for value in value_list]
                for collection_string, value_list in raw_changed_elements.items()
            },
            deleted_elements,
        )

    async def get_element_restricted_data(
        self, user_id: int, collection_string: str, id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Returns the restricted_data of one element.

        Returns None, if the element does not exists or the user has no permission to see it.
        """
        if not self.use_restricted_data_cache:
            full_data = await self.get_element_full_data(collection_string, id)
            if full_data is None:
                return None
            restricter = self.cachables[collection_string].restrict_elements
            restricted_data = await restricter(user_id, [full_data])
            return restricted_data[0] if restricted_data else None

        await self.update_restricted_data(user_id)

        out = await self.cache_provider.get_element(
            get_element_id(collection_string, id), user_id
        )
        return json.loads(out.decode()) if out else None

    async def get_current_change_id(self) -> int:
        """
        Returns the current change id.

        Returns start_time if there is no change id yet.
        """
        value = await self.cache_provider.get_current_change_id()
        if not value:
            return self.start_time
        # Return the score (second element) of the first (and only) element
        return value[0][1]

    async def get_lowest_change_id(self) -> int:
        """
        Returns the lowest change id.

        Raises a RuntimeError if there is no change_id.
        """
        value = await self.cache_provider.get_lowest_change_id()
        if not value:
            raise RuntimeError("There is no known change_id.")
        # Return the score (second element) of the first (and only) element
        return value


def load_element_cache(restricted_data: bool = True) -> ElementCache:
    """
    Generates an element cache instance.
    """
    if use_redis:
        cache_provider_class: Type[ElementCacheProvider] = RedisCacheProvider
    else:
        cache_provider_class = MemmoryCacheProvider

    return ElementCache(
        cache_provider_class=cache_provider_class,
        use_restricted_data_cache=restricted_data,
    )


# Set the element_cache
use_restricted_data = getattr(settings, "RESTRICTED_DATA_CACHE", True)
element_cache = load_element_cache(restricted_data=use_restricted_data)
