import re
import operator

from adminapi.utils import IP
from serveradmin.dataset.base import lookups

_filter_classes = {}

class Filter(object):
    def __and__(self, other):
        return And(self, other)

    def __or__(self, other):
        return Or(self, other)

    def __not__(self):
        return Not(self)

    def __ne__(self, other):
        return not self.__eq__(other)

class ExactMatch(Filter):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return 'ExactMatch({0!r})'.format(self.value)

    def __eq__(self, other):
        if isinstance(other, ExactMatch):
            return self.value == other.value
        return False

    def __hash__(self):
        return hash('ExactMatch') ^ hash(self.value)

    def as_sql_expr(self, attr_name, field):
        return '{0} = {1}'.format(field, _prepare_value(attr_name, self.value))

    def matches(self, server_obj, attr_name):
        return server_obj[attr_name] == self.value

    @classmethod
    def from_obj(cls, obj):
        if 'value' in obj:
            return cls(obj['value'])
        raise ValueError('Invalid object for ExactMatch')
_filter_classes['exactmatch'] = ExactMatch

class Regexp(Filter):
    def __init__(self, regexp):
        self.regexp = regexp

    def __repr__(self):
        return 'Regexp({0!r})'.format(self.regexp)

    def __eq__(self, other):
        if isinstance(other, Regexp):
            return self.regexp == other.regexp
        return False

    def __hash__(self):
        return hash('Regexp') ^ hash(self.regexp)

    def as_sql_expr(self, attr_name, field):
        # XXX Dirty hack for servertype regexp checking
        if attr_name == 'servertype':
            try:
                regexp = re.compile(self.regexp)
            except re.error:
                return '0=1'
            stype_ids = []
            for stype in lookups.stype_ids.itervalues():
                if regexp.search(stype.name):
                    stype_ids.append(stype.pk)
            if stype_ids:
                return '{0} IN({1})'.format(field, ', '.join(stype_ids))
            else:
                return '0=1'
        elif lookups.attr_names[attr_name].type == 'ip':
            return 'NTOA({0}) REGEXP {1}'.format(field, _sql_escape(self.regexp))
        else:
            return '{0} REGEXP {1}'.format(field, _sql_escape(self.regexp))

    def matches(self, server_obj, attr_name):
        value = server_obj[attr_name]
        if lookups.attr_names[attr_name].type == 'ip':
            value = value.as_ip()
        else:
            value = str(value)
        
        return bool(re.search(self.regexp, value))

    @classmethod
    def from_obj(cls, obj):
        if 'regexp' in obj and isinstance(obj['regexp'], basestring):
            return cls(obj['regexp'])
        raise ValueError('Invalid object for Regexp')
_filter_classes['regexp'] = Regexp

class Comparism(Filter):
    def __init__(self, comparator, value):
        if comparator not in ('<', '>', '<=', '>='):
            raise ValueError('Invalid comparism operator: ' + self.comparator)
        self.comparator = comparator
        self.value = value

    def __repr__(self):
        return 'Comparism({0!r}, {1!r})'.format(self.comparator, self.value)

    def __eq__(self, other):
        if isinstance(other, Comparism):
            return (self.comparator == other.comparator and
                    self.value == other.value)
        return False

    def __hash__(self):
        return hash('Comparism') ^ hash(self.comparator) ^ hash(self.value)

    def as_sql_expr(self, attr_name, field):
        return '{0} {1} {2}'.format(field, self.comparator,
                _prepare_value(attr_name, self.value))

    def matches(self, server_obj, attr_name):
        op = {
            '<': operator.lt,
            '>': operator.gt,
            '<=': operator.le,
            '>=': operator.gt
        }[self.comparator]
        return op(server_obj[attr_name], self.value)


    @classmethod
    def from_obj(cls, obj):
        if 'comparator' in obj and 'value' in obj:
            return cls(obj['comparator'], obj['value'])
        raise ValueError('Invalid object for Comparism')
_filter_classes['comparism'] = Comparism

class Any(Filter):
    def __init__(self, *values):
        self.values = set(values)

    def __repr__(self):
        return 'Any({0})'.format(', '.join(repr(val) for val in self.values))

    def __eq__(self, other):
        if isinstance(other, Any):
            return self.values == other.values
        return False

    def __hash__(self):
        h = hash('Any')
        for val in self.values:
            h ^= hash(val)
        return h

    def as_sql_expr(self, attr_name, field):
        if not self.values:
            return '0=1'
        else:
            prepared_values = ', '.join(_prepare_value(attr_name, value)
                    for value in self.values)
            return '{0} IN({1})'.format(field, prepared_values)

    def matches(self, server_obj, attr_name):
        return server_obj[attr_name] in self.values
    
    @classmethod
    def from_obj(cls, obj):
        if 'values' in obj and isinstance(obj['values'], list):
            return cls(*obj['values'])
        raise ValueError('Invalid object for Any')
_filter_classes['any'] = Any

class _AndOr(Filter):
    def __init__(self, *filters):
        self.filters = map(_prepare_filter, filters)

    def __repr__(self):
        args = ', '.join(repr(filter) for filter in self.filters)
        return '{0}({1})'.format(self.name.capitalize(), args)

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.filters == other.filters
        return False

    def __hash__(self):
        h = hash(self.name)
        for val in self.filters:
            h ^= hash(val)
        return h

    def as_sql_expr(self, field):
        joiner = ' {0} '.format(self.name.upper())
        return '({0})'.format(joiner.join([filter.as_sql_expr(field)
                for filter in self.filters]))

    @classmethod
    def from_obj(cls, obj):
        if 'filters' in obj and isinstance(obj['filters'], list):
            return cls(*[filter_from_obj for filter in obj['filters']])
        raise ValueError('Invalid object for {0}'.format(
                cls.__name__.capitalize()))

class And(_AndOr):
    name = 'and'

    def matches(self, server_obj, attr_name):
        for filter in self.filters:
            if not filter.matches(server_obj, attr_name):
                return False
        return True
_filter_classes['and'] = And

class Or(_AndOr):
    name = 'or'
    
    def matches(self, server_obj, attr_name):
        for filter in self.filters:
            if filter.matches(server_obj, attr_name):
                return True
        return False
_filter_classes['or'] = Or

class Between(Filter):
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def __repr__(self):
        return 'Between({0!r}, {1!r})'.format(self.a, self.b)

    def __eq__(self, other):
        if isinstance(other, Between):
            return self.a == other.a and self.b == other.b
        return False

    def __hash__(self):
        return hash('Between') ^ hash(self.a) ^ hash(self.b)

    def as_sql_expr(self, attr_name, field):
        a_prepared = _prepare_value(attr_name, self.a)
        b_prepared = _prepare_value(attr_name, self.b)
        return '{0} BETWEEN {1} AND {2}'.format(field, a_prepared, b_prepared)

    def matches(self, server_obj, attr_name):
        return self.a <= server_obj[attr_name] <= self.b

    @classmethod
    def from_obj(cls, obj):
        if 'a' in obj and 'b' in obj:
            return cls(obj['a'], obj['b'])
        raise ValueError('Invalid object for Between')
_filter_classes['between'] = Between

class Not(Filter):
    def __init__(self, filter):
        self.filter = _prepare_filter(filter)

    def __repr__(self):
        return 'Not({0!})'.format(self.filter)

    def __eq__(self, other):
        if isinstance(other, Not):
            return self.filter == other.filter
        return False

    def __hash__(self):
        return hash('Not') ^ hash(self.filter)

    def as_sql_expr(self, attr_name, field):
        if isinstance(self.filter, ExactMatch):
            return '{0} != {1}'.format(field, _prepare_value(attr_name,
                    self.filter.value))
        else:
            return 'NOT {0}'.format(self.filter.as_sql_expr(attr_name, field))

    def matches(self, server_obj, attr_name):
        return not self.filter.matches(server_obj, attr_name)

    @classmethod
    def from_obj(cls, obj):
        if 'filter' in obj:
            return cls(filter_from_obj(obj['filter']))
        raise ValueError('Invalid object for Not')
_filter_classes['not'] = Not

class Startswith(Filter):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return 'Startswith({0!})'.format(self.value)
    
    def __eq__(self, other):
        if isinstance(other, Startswith):
            return self.value == other.value

    def __hash__(self):
        return hash('Startswith') ^ hash(self.value)

    def as_sql_expr(self, attr_name, field):
        # XXX Dirty hack for servertype checking
        value = self.value.replace('_', '\\_').replace('%', '\\%%')
        if attr_name == 'servertype':
            stype_ids = []
            for stype in lookups.stype_ids.itervalues():
                if stype.name.startswith(self.value):
                    stype_ids.append(stype.pk)
            if stype_ids:
                return '{0} IN({1})'.format(field, ', '.join(stype_ids))
            else:
                return '0=1'
        elif lookups.attr_names[attr_name].type == 'ip':
            return 'NTOA({0}) LIKE {1}'.format(field, _sql_escape(value +
                '%%'))
        else:
            return '{0} LIKE {1}'.format(field, _sql_escape(value + '%%'))

    def matches(self, server_obj, attr_name):
        return unicode(server_obj[attr_name]).startswith(self.value)
    
    @classmethod
    def from_obj(cls, obj):
        if 'value' in obj and isinstance(obj['value'], basestring):
            return cls(obj['value'])
        raise ValueError('Invalid object for Startswith')
_filter_classes['Startswith'] = Startswith


class Optional(object):
    def __init__(self, filter):
        self.filter = _prepare_filter(filter)

    def __repr__(self):
        return 'Optional({0!r})'.format(self.filter)

    def __eq__(self, other):
        if isinstance(other, Optional):
            return self.filter == other.filter
        return False

    def __hash__(self):
        return hash('Optional') ^ hash(self.filter)

    def as_sql_expr(self, attr_name, field):
        return self.filter.as_sql_expr(attr_name, field)

    def matches(self, server_obj, attr_name):
        value = server_obj.get(attr_name)
        if value is None:
            return True
        return self.filter.matches(server_obj, attr_name)

    @classmethod
    def from_obj(cls, obj):
        if 'filter' in obj:
            return cls(filter_from_obj(obj['filter']))
        raise ValueError('Invalid object for Optional')
_filter_classes['optional'] = Optional

def _prepare_filter(filter):
    return filter if isinstance(filter, Filter) else ExactMatch(filter)

def _prepare_value(attr_name, value):
    if lookups.attr_names[attr_name].type == 'ip':
        if not isinstance(value, IP):
            value = IP(value)
        value = value.as_int()
    # XXX: Dirty hack for the old database structure
    if attr_name == 'servertype':
        value = lookups.stype_names[value].pk

    return _sql_escape(value)

def _sql_escape(value):
    if isinstance(value, basestring):
        # FIXME: Prevent SQL Injections using real database escaping
        return "'{0}'".format(value.replace('\\', '\\\\'))
    elif isinstance(value, (int, long, float)):
        return str(value)
    elif isinstance(value, bool):
        return '1' if value else '0'
    else:
        raise ValueError('Value of type {0} can not be used in SQL'.format(
                type(value)))

def filter_from_obj(obj):
    if not (isinstance(obj, dict) and 'name' in obj and
            isinstance(obj['name'], basestring)):
        raise ValueError('Invalid filter object')
    try:
        return _filter_classes[obj['name']].from_obj(obj)
    except KeyError:
        raise ValueError('No such filter: {0}').format(obj['name'])
