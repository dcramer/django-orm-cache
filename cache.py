from django.db import models, backend, connection
from django.db.models.base import ModelBase
from django.db.models.query import QuerySet, GET_ITERATOR_CHUNK_SIZE
from django.core.cache import cache

DEFAULT_CACHE_TIME = 60*60*60 # the maximum an item should be in the cache

# django.db.models.manager
# dispatcher.connect(ensure_default_manager, signal=signals.class_prepared)
# ^-- this is annoying

class CachedModelBase(ModelBase):
    def __new__(cls, name, bases, attrs):
        # If this isn't a subclass of CachedModel, don't do anything special.
        try:
            if not filter(lambda b: issubclass(b, CachedModel), bases):
                return super(CachedModelBase, cls).__new__(cls, name, bases, attrs)
        except NameError:
            # 'CachedModel' isn't defined yet, meaning we're looking at Django's own
            # Model class, defined below.
            return super(CachedModelBase, cls).__new__(cls, name, bases, attrs)

        # Create the class.
        new_class = type.__new__(cls, name, bases, {'__module__': attrs.pop('__module__')})
        new_class.add_to_class('_meta', Options(attrs.pop('Meta', None)))
        new_class.add_to_class('DoesNotExist', types.ClassType('DoesNotExist', (ObjectDoesNotExist,), {}))

        # Build complete list of parents
        for base in bases:
            # TODO: Checking for the presence of '_meta' is hackish.
            if '_meta' in dir(base):
                new_class._meta.parents.append(base)
                new_class._meta.parents.extend(base._meta.parents)


        if getattr(new_class._meta, 'app_label', None) is None:
            # Figure out the app_label by looking one level up.
            # For 'django.contrib.sites.models', this would be 'sites'.
            model_module = sys.modules[new_class.__module__]
            new_class._meta.app_label = model_module.__name__.split('.')[-2]

        # Bail out early if we have already created this class.
        m = get_model(new_class._meta.app_label, name, False)
        if m is not None:
            return m

        # Add all attributes to the class.
        for obj_name, obj in attrs.items():
            new_class.add_to_class(obj_name, obj)

        # Add Fields inherited from parents
        for parent in new_class._meta.parents:
            for field in parent._meta.fields:
                # Only add parent fields if they aren't defined for this class.
                try:
                    new_class._meta.get_field(field.name)
                except FieldDoesNotExist:
                    field.contribute_to_class(new_class, field.name)

        new_class._prepare()

        # now we register the class with the signals it can have turned on;
        signals.pre_init.register_sender(new_class)
        signals.post_init.register_sender(new_class)
        signals.pre_save.register_sender(new_class)
        signals.post_save.register_sender(new_class)

        register_models(new_class._meta.app_label, new_class)
        # Because of the way imports happen (recursively), we may or may not be
        # the first class for this model to register with the framework. There
        # should only be one class for each model, so we must always return the
        # registered version.
        return get_model(new_class._meta.app_label, name, False)

class CachedModel(models.Model):
    """
    docstring for CachedModel
    """
    __metaclass__ = CachedModelBase

    objects = CacheManager()
    nocache = models.Manager()
    
    @staticmethod
    def _get_cache_key_for_pk(model, pk):
        return '%s:%s' % (model._meta.db_table, pk)
    
    def save(self, *args, **kwargs):
        cache.set(self._get_cache_key_for_pk(self.__class__, self.pk), self)
        super(CachedModel, self).save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        cache.delete(self._get_cache_key_for_pk(self.__class__, self.pk))
        super(CachedModel, self).delete(*args, **kwargs)

class CacheManager(models.Manager):
    """
    A manager to store and retrieve cached objects using CACHE_BACKEND

    <string key_prefix> -- the key prefix for all cached objects on this model. [default: db_table]
    <int timeout> -- in seconds, the maximum time before data is invalidated. [default: DEFAULT_CACHE_TIME]
    """
    def __init__(self, *args, **kwargs):
        self.key_prefix = kwargs.pop('key_prefix', None)
        self.timeout = kwargs.pop('timeout', None)
        super(CacheManager, self).__init__(*args, **kwargs)

    def get_query_set(self):
        return CachedQuerySet(model=self.model, timeout=self.timeout, key_prefix=self.key_prefix)

    def cache(self, *args, **kwargs):
        return self.get_query_set().cache(*args, **kwargs)

    def clean(self, *args, **kwargs):
        return self.get_query_set().clean(*args, **kwargs)

    def reset(self, *args, **kwargs):
        return self.get_query_set().reset(*args, **kwargs)


# TODO: if the query is passing pks then we need to make it pull the cache key from the model
# and try to fetch that first
# if there are additional filters to apply beyond pks we then filter those after we're already pulling the pks

# TODO: should we also run these additional filters each time we pull back a ref list to check for validation?

# TODO: all related field calls need to be removed and replaced with cache key sets of some sorts
# (just remove the join and make it do another qs.filter(pk__in) to pull them, which would do a many cache get callb)

class CachedQuerySet(QuerySet):
    """
    Extends the QuerySet object and caches results via CACHE_BACKEND.
    """
    def __init__(self, model=None, key_prefix=None, timeout=None, *args, **kwargs):
        self._cache_keys = {}
        self._cache_reset = False
        self._cache_clean = False
        if key_prefix:
            self.cache_key_prefix = key_prefix
        else:
            if model:
                self.cache_key_prefix = model._meta.db_table
            else:
                self.cache_key_prefix = ''
        if timeout:
            self.cache_timeout = timeout
        else:
            self.cache_timeout = getattr(cache, 'default_timeout', DEFAULT_CACHE_TIME)
        QuerySet.__init__(self, model, *args, **kwargs)

    def _clone(self, klass=None, **kwargs):
        c = QuerySet._clone(self, klass, **kwargs)
        c._cache_clean = kwargs.pop('_cache_clean', self._cache_clean)
        c._cache_reset = kwargs.pop('_cache_reset', self._cache_reset)
        c.cache_key_prefix = kwargs.pop('cache_key_prefix', self.cache_key_prefix)
        c.cache_timeout = kwargs.pop('cache_timeout', self.cache_timeout)
        c._cache_keys = {}
        return c

    def _get_sorted_clause_key(self):
        return (isinstance(i, basestring) and i.lower().replace('`', '').replace("'", '') or str(tuple(sorted(i))) for i in self._get_sql_clause())

    def _get_cache_key(self, extra=''):
        if extra not in self._cache_keys:
            self._cache_keys[extra] = '%s%s%s' % (self.cache_key_prefix, str(hash(''.join(self._get_sorted_clause_key()))), extra)
        return self._cache_keys[extra]

    def _get_data(self):
        ck = self._get_cache_key()
        if self._result_cache is None or self._cache_clean or self._cache_reset:
            if self._cache_clean:
                cache.delete(ck)
                return
            self._result_cache = cache.get(ck)
            if self._result_cache is None or self._cache_reset:
                self._result_cache = QuerySet._get_data(self)
                self._cache_reset = False
                cache.set(ck, self._result_cache, self.cache_timeout*60)
        return self._result_cache

    def execute(self):
        """
        Forces execution on the queryset
        """
        self._get_data()
        return self

    def get(self, *args, **kwargs):
        "Performs the SELECT and returns a single object matching the given keyword arguments."
        if self._cache_clean:
            clone = self.filter(*args, **kwargs)
            if not clone._order_by:
                clone._order_by = ()
            cache.delete(self._get_cache_key())
        else:
            return QuerySet.get(self, *args, **kwargs)

    def clean(self):
        """
        Removes queryset from the cache -- recommended to use <CacheManager instance>.clean()
        """
        return self._clone(_cache_clean=True)

    def count(self):
        return QuerySet.count(self)
        count = cache.get(self._get_cache_key('count'))
        if count is None:
            count = int(QuerySet.count(self))
            cache.set(self._get_cache_key('count'), count, self.cache_timeout)
        return count

    def cache(self, *args, **kwargs):
        """
        Overrides CacheManager's options for this QuerySet.

        (Optional) <string key_prefix> -- the key prefix for all cached objects on this model. [default: db_table]
        (Optional) <int timeout> -- in seconds, the maximum time before data is invalidated.
        """
        return self._clone(cache_key_prefix=kwargs.pop('key_prefix', self.cache_key_prefix), cache_timeout=kwargs.pop('timeout', self.cache_timeout))

    def reset(self):
        """
        Updates the queryset in the cache upon execution.
        """
        return self._clone(_cache_reset=True)

    def values(self, *fields):
        return self._clone(klass=CachedValuesQuerySet, _fields=fields)

# need a better way to do this.. (will mix-ins work?)
class CachedValuesQuerySet(CachedQuerySet):
    def __init__(self, *args, **kwargs):
        super(CachedQuerySet, self).__init__(*args, **kwargs)
        # select_related isn't supported in values().
        self._select_related = False

    def iterator(self):
        try:
            select, sql, params = self._get_sql_clause()
        except EmptyResultSet:
            raise StopIteration

        # self._fields is a list of field names to fetch.
        if self._fields:
            #columns = [self.model._meta.get_field(f, many_to_many=False).column for f in self._fields]
            if not self._select:
                columns = [self.model._meta.get_field(f, many_to_many=False).column for f in self._fields]
            else:
                columns = []
                for f in self._fields:
                    if f in [field.name for field in self.model._meta.fields]:
                        columns.append( self.model._meta.get_field(f, many_to_many=False).column )
                    elif not self._select.has_key( f ):
                        raise FieldDoesNotExist, '%s has no field named %r' % ( self.model._meta.object_name, f )

            field_names = self._fields
        else: # Default to all fields.
            columns = [f.column for f in self.model._meta.fields]
            field_names = [f.column for f in self.model._meta.fields]

        select = ['%s.%s' % (backend.quote_name(self.model._meta.db_table), backend.quote_name(c)) for c in columns]

        # Add any additional SELECTs.
        if self._select:
            select.extend(['(%s) AS %s' % (quote_only_if_word(s[1]), backend.quote_name(s[0])) for s in self._select.items()])

        if getattr(self, '_db_use_master', False):
            cursor = connection.write_cursor()
        else:
            cursor = connection.read_cursor()
        cursor.execute("SELECT " + (self._distinct and "DISTINCT " or "") + ",".join(select) + sql, params)
        while 1:
            rows = cursor.fetchmany(GET_ITERATOR_CHUNK_SIZE)
            if not rows:
                raise StopIteration
            for row in rows:
                yield dict(zip(field_names, row))

    def _clone(self, klass=None, **kwargs):
        c = super(CachedValuesQuerySet, self)._clone(klass, **kwargs)
        c._fields = self._fields[:]
        return c