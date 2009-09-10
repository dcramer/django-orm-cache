Django ORM Cache
================

This project is an attempt to provide a low-level, somewhat magical, approach to handling row-level object caches.

The project page, including all descriptions and reference material, is still very much a work in progress. If you are interested in contributing to the projects, in code, in ideas, or fixing our ever so awesome project outline, please get in touch with one of the project admins (see below).

Summary
-------

In brief, it's goal is to store only unique objects in a cache key, instead of storing groups of objects in many different locations. This would make updates to a cache object as simple as updating a single cache instance, and also automatically handle updating any lists that hold that object when it is removed.

Approach
--------

The approach consists of several major pieces of functionality:

* Unique row-level object cache keys.
* Lists of cache keys (e.g. a QuerySet).
* Model-based dependency via signals.
* Row-based dependency via reverse mapping.

The caching layer will also provide a few additional items:

* Handling cache stampedes via pre-expiration timestamps.
* Namespace versioning and expirations.

Row-level Caches
----------------

The primary goal of the project was originally to handle invalidation of many instances of a single object. Typical caching setups would have you create copies of a single object, and place it in many cache keys. The goal of this project is to replace those many copies with a single unique instance, thus making updates and invalidation a much simpler task.

CachedModel
-----------

The row-level cache is primarily managed by the ``CachedModel`` class. This class override several key methods on the normal Model class:

* save() -- upon saving it automatically updates the cache instance.
* delete() -- upon delete (optional maybe? useless in memcache) it removes the cache instance.
* objects -- a CacheManager instance.
* nocache -- the default manager instance.

Usage::

	from ormcache.models import CachedModel
	class Article(CachedModel):
	    ...

QuerySet Caches
---------------

One key problem with having a unique instance of a cache is managing pointers to that instance, as well as the efficiency of those pointers. This approach will simply store a set of pointers (primary keys) to which the backend would automatically query for and invalidate as needed.

CacheManager
------------

The QuerySet caching consists of one key component, the CacheManager. It's responsibility is to handle the unique storage and retrieval of QuerySet caches. It is also in charge of informing the parent class (CachedModel) of any invalidation warnings.

The code itself should work exactly like the default Manager and QuerySet with a few additional methods:

* clean() -- removes the queryset; executes but does not perform any SQL or cache.get methods).
* reset() -- resets the queryset; executes the sql and updates the cache.
* execute() -- forces execution of the current query; no return value.
* cache(key=<string>, timeout=<minutes>) -- changes several options of the current cache instance.

One thing to note is that we may possibly be able to get rid of clean, or merge it with reset. The execute() method exists because reset does not force execution of the queryset (maybe this should be changed?).

Fetching Sets
-------------

The biggest use of the CacheManager will come in the former of sets. A set is simply a list of cache key pointers. For efficieny this will be stored in a custom format for Django models::

	(ModelClass, (*pks), (*select_related fields), key length)

ModelClass
##########

The first item in our cache key is the ModelClass in which the list is pointing to. We may need a way to handle unions but that's up to further discussion. The ModelClass is needed to reference where the pks go.

Pointers
########
The second item, is our list of pointers, or primary keys in this use-case. This could be anything from a list of your standard id integers, to a group of long strings.

Relations
#########

The third item, our select_related fields. These are needed to ensure that we can follow the depth when querying.

e.g.
We do MyModel.objects.all().select_related('user'). We then fetch the key which says its these 10 pks, from MyModel. We now need to also know that user was involved so we can fetch that in batch (vs it automatically doing 1 cache call per key).

So the route would be:

* Pull MyModel's cache key.
* Batch pull all pks referenced from queryset.
* Group all fields by their ModelClass, and then either:
  1. Do a cachedqueryset call on them (which would be looking up another list of values, possibly reusable, bad idea?)
  2. Pull (as much at a time) the caches in bulk.

Upon failure of any cache pull it would fall back to the ORM, request all the rows it can (for a single model, in a single query), and then set those in the cache. If any one of those rows was not found in the database, it would throw up an Invalidation Warning, which, by default, would expire that parent object and repoll the database for it's dataset.

Key Length
##########

The key length is something which was brought up recently. Memcached has limits to how much you can store in a single key. We can work around those. The key length would simply tell the CacheManager how many keys are used for that single list.

Model Dependencies
------------------

Model dependencies would be handling via registration in a way such as how signals are handled.

Object Dependencies
-------------------

Object dependencies will be handled by storing a reverse mapping to keys. These dependencies would simply be attached (in a separate key) to the original object in which they are dependent on.

References
----------

* [http://www.davidcramer.net/code/61/handling-cache-invalidation.html Handling Cache Invalidation] by David Cramer