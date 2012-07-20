filter_classes = {}

class ExactMatch(object):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return 'ExactMatch({0!r})'.format(self.value)

    def _serialize(self):
        return {'name': 'exactmatch', 'value': self.value}
filter_classes['exactmatch'] = ExactMatch


class Regexp(object):
    def __init__(self, regexp):
        self.regexp = regexp

    def __repr__(self):
        return 'Regexp({0!r})'.format(self.regexp)

    def _serialize(self):
        return {'name': 'regexp', 'regexp': self.regexp}
filter_classes['regexp'] = Regexp


class Comparison(object):
    def __init__(self, comparator, value):
        if comparator not in ('<', '>', '<=', '>='):
            raise ValueError('Invalid comparism operator: ' + comparator)
        self.comparator = comparator
        self.value = value

    def __repr__(self):
        return 'Comparism({0!r}, {1!r})'.format(self.comparator, self.value)

    def _serialize(self):
        return {'name': 'comparison', 'comparator': self.comparator,
                'value': self.value}
filter_classes['comparison'] = Comparison
        
Comparism = Comparison # Backward compatibilty


class Any(object):
    def __init__(self, *values):
        self.values = values

    def __repr__(self):
        return 'Any({0})'.format(', '.join(repr(val) for val in self.values))

    def _serialize(self):
        return {'name': 'any', 'values': self.values}
filter_classes['any'] = Any


class _AndOr(object):
    def __init__(self, *filters):
        self.filters = map(_prepare_filter, filters)

    def __repr__(self):
        args = ', '.join(repr(filter) for filter in self.filters)
        return '{0}({1})'.format(self.name.capitalize(), args)

    def _serialize(self):
        return {'name': self.name, 'filters': [f._serialize() for f in
            self.filters]}


class And(_AndOr):
    name = 'and'
filter_classes['and'] = And


class Or(_AndOr):
    name = 'or'
filter_classes['or'] = Or


class Between(object):
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def __repr__(self):
        return 'Between({0!r}, {1!r})'.format(self.a, self.b)

    def _serialize(self):
        return {'name': 'between', 'a': self.a, 'b': self.b}
filter_classes['between'] = Between


class Not(object):
    def __init__(self, filter):
        self.filter = _prepare_filter(filter)

    def __repr__(self):
        return 'Not({0!r})'.format(self.filter)
    
    def _serialize(self):
        return {'name': 'not', 'filter': self.filter._serialize()}
filter_classes['not'] = Not


class Startswith(object):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return 'Startswith({0!r})'.format(self.value)

    def _serialize(self):
        return {'name': 'startswith', 'value': self.value}
filter_classes['startswith'] = Startswith


class Optional(object):
    def __init__(self, filter):
        self.filter = _prepare_filter(filter)

    def __repr__(self):
        return 'Optional({0!r})'.format(self.filter)

    def _serialize(self):
        return {'name': 'optional', 'filter': self.filter._serialize()}
filter_classes['optional'] = Optional


def _prepare_filter(filter):
    return filter if hasattr(filter, '_serialize') else ExactMatch(filter)
