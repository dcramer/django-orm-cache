from django.db.models.base import ModelBase
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
    
    @property
    def cache_key(self):
        return self._get_cache_key_for_pk(self.__class__, self.pk)
    
    def save(self, *args, **kwargs):
        cache.set(self._get_cache_key_for_pk(self.__class__, self.pk), self)
        super(CachedModel, self).save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # TODO: create an option that tells the model whether or not it should
        # do a cache.delete when the object is deleted. For memcached we
        # wouldn't care about deleting.
        cache.delete(self._get_cache_key_for_pk(self.__class__, self.pk))
        super(CachedModel, self).delete(*args, **kwargs)