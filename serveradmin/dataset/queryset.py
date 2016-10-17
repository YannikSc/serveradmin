from collections import defaultdict, OrderedDict

from django.core.exceptions import ValidationError

from adminapi.dataset.base import BaseQuerySet, BaseServerObject

from serveradmin.serverdb.models import (
    Project,
    Servertype,
    Attribute,
    ServertypeAttribute,
    Server,
    ServerAttribute,
    ServerHostnameAttribute,
)
from serveradmin.dataset.commit import commit_changes
from serveradmin.dataset.filters import ExactMatch, Any
from serveradmin.dataset.querybuilder import QueryBuilder

CACHE_MIN_QS_COUNT = 3
NUM_OBJECTS_FOR_FILECACHE = 50


class QuerySetRepresentation(object):
    """Object that can be easily pickled without storing to much data.
    """

    def __init__(
        self,
        filters,
        restrict,
        augmentations,
        order_by,
        order_dir,
    ):
        self.filters = filters
        self.restrict = restrict
        self.augmentations = augmentations
        self.order_by = order_by
        self.order_dir = order_dir

    def __hash__(self):
        h = 0
        if self.restrict:
            for val in self.restrict:
                h ^= hash(val)
        if self.augmentations:
            for val in self.augmentations:
                h ^= hash(val)
        for attr_name, attr_filter in self.filters.items():
            h ^= hash(attr_name)
            h ^= hash(attr_filter)

        if self.order_by:
            h ^= hash(self.order_by)
            h ^= hash(self.order_dir)

        return h

    def __eq__(self, other):
        if not isinstance(other, QuerySetRepresentation):
            return False

        if self.restrict and other.restrict:
            if set(self.restrict) - set(other.restrict):
                return False
        elif self.restrict or other.restrict:
            return False

        if self.augmentations and other.augmentations:
            if set(self.augmentations) - set(other.augmentations):
                return False
        elif self.augmentations or other.augmentations:
            return False

        if len(self.filters) != len(other.filters):
            return False

        for key in self.filters:
            if key not in other.filters:
                return False
            if self.filters[key] != other.filters[key]:
                return False

        if self.order_by != other.order_by:
            return False

        if self.order_dir != other.order_dir:
            return False

        return True

    def as_code(self):
        args = []
        for attr_name, value in self.filters.items():
            args.append('{0}={1}'.format(attr_name, value.as_code()))
        return 'query({0})'.format(', '.join(args))


class QuerySet(BaseQuerySet):
    def __init__(self, filters):
        self._filters = {}
        for attribute_id, filter_obj in filters.items():
            try:
                attribute = Attribute.objects.get(pk=attribute_id)
            except Attribute.DoesNotExist:
                raise ValidationError(
                    'Invalid attribute: {0}'.format(attribute_id)
                )
            filter_obj.typecast(attribute)
            self._filters[attribute] = filter_obj
        self._servertype_attribute = Attribute.specials['servertype']
        self._restrict = set()
        self._results = None
        self._augmentations = None
        self._num_dirty = 0
        self._order = None
        self._order_by = None
        self._order_dir = 'asc'

    def commit(self, *args, **kwargs):
        commit = self._build_commit_object()
        commit_changes(commit, *args, **kwargs)
        self._confirm_changes()

    def get_representation(self):
        return QuerySetRepresentation(
            self._filters,
            self._restrict,
            self._augmentations,
            self._order_by,
            self._order_dir,
        )

    def restrict(self, *attrs):
        for attribute_id in attrs:
            try:
                self._restrict.add(Attribute.objects.get(pk=attribute_id))
            except Attribute.DoesNotExist:
                raise ValidationError(
                    'Invalid attribute: {0}'.format(attribute_id)
                )
        return self

    def order_by(self, order_by, order_dir='asc'):
        try:
            self._order_by = Attribute.objects.get(pk=order_by)
        except Attribute.DoesNotExist:
            raise ValidationError(
                'Invalid attribute: {0}'.format(order_by)
            )

        if order_dir not in ('asc', 'desc'):
            raise ValueError('Invalid order direction')
        self._order_dir = order_dir

        # We need the attribute to order by.
        if self._restrict:
            self._restrict.add(order_by)

        return self

    def _get_query_builder_with_filters(self):
        attributes = []
        builder = QueryBuilder()
        servertypes = set(Servertype.objects.all())
        projects = Project.objects.all()
        for attribute, filt in self._filters.items():
            if attribute.pk == 'intern_ip' and isinstance(filt, ExactMatch):
                # Filter out servertypes depending on ip_addr_type
                is_network = '/' in str(filt.value)
                servertypes = {
                    s for s in servertypes
                    if (s.ip_addr_type == 'network') == is_network
                }
            elif attribute.pk == 'servertype':
                servertypes = {s for s in servertypes if filt.matches(s.pk)}
            elif attribute.pk == 'project':
                projects = [p for p in projects if filt.matches(p.pk)]
                # Filter out servertype with inconsistent fixed_project
                servertypes = {
                    s for s in servertypes
                    if s.fixed_project is None or s.fixed_project in projects
                }
            elif not attribute.special:
                attributes.append(attribute)

        if attributes:
            attribute_servertypes = defaultdict(set)
            for sa in ServertypeAttribute.query(attributes=attributes).all():
                attribute_servertypes[sa.attribute].add(sa.servertype)
            for new in attribute_servertypes.values():
                servertypes = servertypes.intersection(new)

        if len(servertypes) < len(Servertype.objects.all()):
            if not servertypes:
                return None
            builder.add_filter(
                Attribute.objects.get(pk='servertype'),
                servertypes,
                Any(*(s.pk for s in servertypes)),
            )
        if len(projects) < len(Project.objects.all()):
            if not projects:
                return None
            builder.add_filter(
                Attribute.objects.get(pk='project'),
                projects,
                Any(*(p.pk for p in projects)),
            )

        for attribute, filt in self._filters.items():
            if attribute.pk not in ('project', 'servertype'):
                builder.add_filter(attribute, servertypes, filt)

        return builder

    def _fetch_results(self):
        builder = self._get_query_builder_with_filters()
        if builder is None:
            self._results = {}
            return

        self._server_attributes = dict()
        servers_by_type = defaultdict(list)
        servers = tuple(Server.objects.raw(builder.build_sql()))
        for server in servers:
            self._server_attributes[server.pk] = {
                Attribute.specials['hostname']: server.hostname,
                Attribute.specials['intern_ip']: server.intern_ip,
                Attribute.specials['segment']: server.segment,
                Attribute.specials['servertype']: server.servertype,
                Attribute.specials['project']: server.project,
            }
            servers_by_type[server.servertype].append(server)

        self._select_attributes(servers_by_type.keys())
        self._add_attributes(servers_by_type)

        result_class = dict
        server_ids = self._server_attributes.keys()
        if self._order_by:
            result_class = OrderedDict
            # We need to sort by attribute being there first, because some
            # datatypes are not sortable with None.
            server_ids = sorted(server_ids, key=lambda i: (
                self._order_by in self._server_attributes[i],
                self._server_attributes[i].get(self._order_by),
            ))

        self._results = result_class(
            (i, ServerObject(self._get_attributes(i), i, self))
            for i in server_ids
        )

    def _select_attributes(self, servertypes):
        self._attributes_by_type = defaultdict(set)
        self._servertypes_by_attribute = defaultdict(list)
        self._related_servertype_attributes = []
        attributes = self._restrict if self._restrict else None
        for sa in ServertypeAttribute.query(servertypes, attributes).all():
            self._select_servertype_attribute(sa)

    def _select_servertype_attribute(self, sa):
        self._attributes_by_type[sa.attribute.type].add(sa.attribute)
        self._servertypes_by_attribute[sa.attribute].append(sa.servertype)

        related_via_attribute = sa.related_via_attribute
        if related_via_attribute:
            # TODO Order the list in a way to support recursive related
            # attributes.
            self._related_servertype_attributes.append(sa)

            # If we have related attributes in the restrict list, we have
            # to add the relations in there, too.  We are going to use
            # those to query the related attributes.
            if self._restrict and related_via_attribute not in self._restrict:
                self._select_servertype_attribute(ServertypeAttribute.query(
                    (sa.servertype, ), (related_via_attribute, )
                ).get())

    def _add_attributes(self, servers_by_type):
        """Add the attributes to the results"""

        # Step 0: Initialize the multi attributes
        for attribute, servertypes in self._servertypes_by_attribute.items():
            if attribute.multi:
                for servertype in servertypes:
                    for server in servers_by_type[servertype]:
                        self._server_attributes[server.pk][attribute] = set()

        # Step 1: Query the materialized attributes by their types
        for key, attributes in self._attributes_by_type.items():
            if key == 'supernet':
                for attribute in attributes:
                    self._add_supernet_attribute(attribute, (
                        s for st in self._servertypes_by_attribute[attribute]
                        for s in servers_by_type[st]
                    ))
            elif key == 'reverse_hostname':
                reversed_attributes = {
                    a.reversed_attribute: a for a in attributes
                }
                for sa in ServerHostnameAttribute.objects.filter(
                    value_id__in=self._server_attributes.keys(),
                    _attribute__in=reversed_attributes.keys(),
                ).select_related('server'):
                    self._add_attribute_value(
                        sa.value_id,
                        reversed_attributes[sa.attribute],
                        sa.server.hostname
                    )
            else:
                for sa in ServerAttribute.get_model(key).objects.filter(
                    server_id__in=self._server_attributes.keys(),
                    _attribute__in=attributes,
                ):
                    self._add_attribute_value(
                        sa.server_id, sa.attribute, sa.get_value()
                    )

        # Step 2: Add the related attributes
        for servertype_attribute in self._related_servertype_attributes:
            self._add_related_attribute(servertype_attribute, servers_by_type)

    def _add_supernet_attribute(self, attribute, servers):
        """Merge-join networks to the servers

        This function takes advantage of networks in the same servertype not
        overlapping with each other.
        """
        query = Server.objects.values_list('intern_ip', 'hostname').filter(
            _servertype=attribute.target_servertype
        )
        target = None
        for source in sorted(servers, key=lambda s: s.intern_ip):
            # Check the previous target
            if target is not None:
                network = target[0].network
                if network.broadcast_address < source.intern_ip.ip:
                    target = None
                elif source.intern_ip not in network:
                    continue
            # Check for a new target
            if target is None:
                try:
                    target = query.get(
                        intern_ip__net_contains_or_equals=source.intern_ip
                    )
                except Server.DoesNotExist:
                    continue
            self._server_attributes[source.pk][attribute] = target[1]

    def _add_related_attribute(self, servertype_attribute, servers_by_type):
        attribute = servertype_attribute.attribute
        related_via_attribute = servertype_attribute.related_via_attribute

        # First, index the related servers for fast access later
        servers_by_related_hostnames = defaultdict(list)
        for server in servers_by_type[servertype_attribute.servertype]:
            attributes = self._server_attributes[server.pk]
            if related_via_attribute in attributes:
                if related_via_attribute.multi:
                    for hostname in attributes[related_via_attribute]:
                        servers_by_related_hostnames[hostname].append(server)
                else:
                    hostname = attributes[related_via_attribute]
                    servers_by_related_hostnames[hostname].append(server)

        # Then, query and set the related attributes
        for sa in ServerAttribute.get_model(attribute.type).objects.filter(
            server__hostname__in=servers_by_related_hostnames.keys(),
            _attribute=attribute,
        ).select_related('server'):
            for server in servers_by_related_hostnames[sa.server.hostname]:
                self._add_attribute_value(
                    server.pk, sa.attribute, sa.get_value()
                )

    def _add_attribute_value(self, server_id, attribute, value):
        if attribute.multi:
            self._server_attributes[server_id][attribute].add(value)
        else:
            self._server_attributes[server_id][attribute] = value

    def _get_attributes(self, server_id):
        server_attributes = self._server_attributes[server_id]
        for attribute, value in server_attributes.items():
            if not self._restrict or attribute in self._restrict:
                if attribute.pk in ('project', 'servertype', 'segment'):
                    yield attribute.pk, value.pk
                elif attribute.pk == 'intern_ip':
                    servertype = server_attributes[self._servertype_attribute]
                    if servertype.ip_addr_type == 'null':
                        yield attribute.pk, None
                    elif servertype.ip_addr_type in ('host', 'loadbalancer'):
                        yield attribute.pk, value.ip
                    else:
                        assert servertype.ip_addr_type == 'network'
                        yield attribute.pk, value.network
                else:
                    yield attribute.pk, value


class ServerObject(BaseServerObject):
    def commit(self, app=None, user=None):
        commit = self._build_commit_object()
        commit_changes(commit, app=app, user=user)
        self._confirm_changes()

    def __reduce__(self):
        # Just pickle it as normal dict
        tpl = dict.__reduce__(self)
        instance_dict = tpl[2].copy()
        del instance_dict['_queryset']
        return (tpl[0], tpl[1], instance_dict)
