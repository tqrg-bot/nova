# Copyright 2011 OpenStack LLC.
# Copyright 2012 Justin Santa Barbara
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""The security groups extension."""

from xml.dom import minidom

import webob
from webob import exc

from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova import compute
from nova import db
from nova import exception
from nova import flags
from nova.openstack.common import excutils
from nova.openstack.common import log as logging
from nova import utils


LOG = logging.getLogger(__name__)
FLAGS = flags.FLAGS
authorize = extensions.extension_authorizer('compute', 'security_groups')


def make_rule(elem):
    elem.set('id')
    elem.set('parent_group_id')

    proto = xmlutil.SubTemplateElement(elem, 'ip_protocol')
    proto.text = 'ip_protocol'

    from_port = xmlutil.SubTemplateElement(elem, 'from_port')
    from_port.text = 'from_port'

    to_port = xmlutil.SubTemplateElement(elem, 'to_port')
    to_port.text = 'to_port'

    group = xmlutil.SubTemplateElement(elem, 'group', selector='group')
    name = xmlutil.SubTemplateElement(group, 'name')
    name.text = 'name'
    tenant_id = xmlutil.SubTemplateElement(group, 'tenant_id')
    tenant_id.text = 'tenant_id'

    ip_range = xmlutil.SubTemplateElement(elem, 'ip_range',
                                          selector='ip_range')
    cidr = xmlutil.SubTemplateElement(ip_range, 'cidr')
    cidr.text = 'cidr'


def make_sg(elem):
    elem.set('id')
    elem.set('tenant_id')
    elem.set('name')

    desc = xmlutil.SubTemplateElement(elem, 'description')
    desc.text = 'description'

    rules = xmlutil.SubTemplateElement(elem, 'rules')
    rule = xmlutil.SubTemplateElement(rules, 'rule', selector='rules')
    make_rule(rule)


sg_nsmap = {None: wsgi.XMLNS_V11}


class SecurityGroupRuleTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('security_group_rule',
                                       selector='security_group_rule')
        make_rule(root)
        return xmlutil.MasterTemplate(root, 1, nsmap=sg_nsmap)


class SecurityGroupTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('security_group',
                                       selector='security_group')
        make_sg(root)
        return xmlutil.MasterTemplate(root, 1, nsmap=sg_nsmap)


class SecurityGroupsTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('security_groups')
        elem = xmlutil.SubTemplateElement(root, 'security_group',
                                          selector='security_groups')
        make_sg(elem)
        return xmlutil.MasterTemplate(root, 1, nsmap=sg_nsmap)


class SecurityGroupXMLDeserializer(wsgi.MetadataXMLDeserializer):
    """
    Deserializer to handle xml-formatted security group requests.
    """
    def default(self, string):
        """Deserialize an xml-formatted security group create request"""
        dom = minidom.parseString(string)
        security_group = {}
        sg_node = self.find_first_child_named(dom,
                                               'security_group')
        if sg_node is not None:
            if sg_node.hasAttribute('name'):
                security_group['name'] = sg_node.getAttribute('name')
            desc_node = self.find_first_child_named(sg_node,
                                                     "description")
            if desc_node:
                security_group['description'] = self.extract_text(desc_node)
        return {'body': {'security_group': security_group}}


class SecurityGroupRulesXMLDeserializer(wsgi.MetadataXMLDeserializer):
    """
    Deserializer to handle xml-formatted security group requests.
    """

    def default(self, string):
        """Deserialize an xml-formatted security group create request"""
        dom = minidom.parseString(string)
        security_group_rule = self._extract_security_group_rule(dom)
        return {'body': {'security_group_rule': security_group_rule}}

    def _extract_security_group_rule(self, node):
        """Marshal the security group rule attribute of a parsed request"""
        sg_rule = {}
        sg_rule_node = self.find_first_child_named(node,
                                                   'security_group_rule')
        if sg_rule_node is not None:
            ip_protocol_node = self.find_first_child_named(sg_rule_node,
                                                           "ip_protocol")
            if ip_protocol_node is not None:
                sg_rule['ip_protocol'] = self.extract_text(ip_protocol_node)

            from_port_node = self.find_first_child_named(sg_rule_node,
                                                         "from_port")
            if from_port_node is not None:
                sg_rule['from_port'] = self.extract_text(from_port_node)

            to_port_node = self.find_first_child_named(sg_rule_node, "to_port")
            if to_port_node is not None:
                sg_rule['to_port'] = self.extract_text(to_port_node)

            parent_group_id_node = self.find_first_child_named(sg_rule_node,
                                                            "parent_group_id")
            if parent_group_id_node is not None:
                sg_rule['parent_group_id'] = self.extract_text(
                                                         parent_group_id_node)

            group_id_node = self.find_first_child_named(sg_rule_node,
                                                        "group_id")
            if group_id_node is not None:
                sg_rule['group_id'] = self.extract_text(group_id_node)

            cidr_node = self.find_first_child_named(sg_rule_node, "cidr")
            if cidr_node is not None:
                sg_rule['cidr'] = self.extract_text(cidr_node)

        return sg_rule


class SecurityGroupControllerBase(object):
    """Base class for Security Group controllers."""

    def __init__(self):
        self.security_group_api = NativeSecurityGroupAPI()
        self.compute_api = compute.API(
                                   security_group_api=self.security_group_api)

    def _format_security_group_rule(self, context, rule):
        sg_rule = {}
        sg_rule['id'] = rule.id
        sg_rule['parent_group_id'] = rule.parent_group_id
        sg_rule['ip_protocol'] = rule.protocol
        sg_rule['from_port'] = rule.from_port
        sg_rule['to_port'] = rule.to_port
        sg_rule['group'] = {}
        sg_rule['ip_range'] = {}
        if rule.group_id:
            source_group = self.security_group_api.get(context,
                                                       id=rule.group_id)
            sg_rule['group'] = {'name': source_group.name,
                             'tenant_id': source_group.project_id}
        else:
            sg_rule['ip_range'] = {'cidr': rule.cidr}
        return sg_rule

    def _format_security_group(self, context, group):
        security_group = {}
        security_group['id'] = group.id
        security_group['description'] = group.description
        security_group['name'] = group.name
        security_group['tenant_id'] = group.project_id
        security_group['rules'] = []
        for rule in group.rules:
            security_group['rules'] += [self._format_security_group_rule(
                    context, rule)]
        return security_group

    def _authorize_context(self, req):
        context = req.environ['nova.context']
        authorize(context)
        return context

    def _validate_id(self, id):
        try:
            return int(id)
        except ValueError:
            msg = _("Security group id should be integer")
            raise exc.HTTPBadRequest(explanation=msg)

    def _from_body(self, body, key):
        if not body:
            raise exc.HTTPUnprocessableEntity()
        value = body.get(key, None)
        if value is None:
            raise exc.HTTPUnprocessableEntity()
        return value


class SecurityGroupController(SecurityGroupControllerBase):
    """The Security group API controller for the OpenStack API."""

    @wsgi.serializers(xml=SecurityGroupTemplate)
    def show(self, req, id):
        """Return data about the given security group."""
        context = self._authorize_context(req)

        id = self._validate_id(id)

        security_group = self.security_group_api.get(context, None, id,
                                                     map_exception=True)

        return {'security_group': self._format_security_group(context,
                                                              security_group)}

    def delete(self, req, id):
        """Delete a security group."""
        context = self._authorize_context(req)

        id = self._validate_id(id)

        security_group = self.security_group_api.get(context, None, id,
                                                     map_exception=True)

        self.security_group_api.destroy(context, security_group)

        return webob.Response(status_int=202)

    @wsgi.serializers(xml=SecurityGroupsTemplate)
    def index(self, req):
        """Returns a list of security groups"""
        context = self._authorize_context(req)

        raw_groups = self.security_group_api.list(context,
                                                  project=context.project_id)

        limited_list = common.limited(raw_groups, req)
        result = [self._format_security_group(context, group)
                     for group in limited_list]

        return {'security_groups':
                list(sorted(result,
                            key=lambda k: (k['tenant_id'], k['name'])))}

    @wsgi.serializers(xml=SecurityGroupTemplate)
    @wsgi.deserializers(xml=SecurityGroupXMLDeserializer)
    def create(self, req, body):
        """Creates a new security group."""
        context = self._authorize_context(req)

        security_group = self._from_body(body, 'security_group')

        group_name = security_group.get('name', None)
        group_description = security_group.get('description', None)

        self.security_group_api.validate_property(group_name, 'name', None)
        self.security_group_api.validate_property(group_description,
                                                  'description', None)

        group_ref = self.security_group_api.create(context, group_name,
                                                   group_description)

        return {'security_group': self._format_security_group(context,
                                                                 group_ref)}


class SecurityGroupRulesController(SecurityGroupControllerBase):

    @wsgi.serializers(xml=SecurityGroupRuleTemplate)
    @wsgi.deserializers(xml=SecurityGroupRulesXMLDeserializer)
    def create(self, req, body):
        context = self._authorize_context(req)

        sg_rule = self._from_body(body, 'security_group_rule')

        parent_group_id = self._validate_id(sg_rule.get('parent_group_id',
                                                        None))

        security_group = self.security_group_api.get(context, None,
                                          parent_group_id, map_exception=True)

        try:
            values = self._rule_args_to_dict(context,
                              to_port=sg_rule.get('to_port'),
                              from_port=sg_rule.get('from_port'),
                              ip_protocol=sg_rule.get('ip_protocol'),
                              cidr=sg_rule.get('cidr'),
                              group_id=sg_rule.get('group_id'))
        except Exception as exp:
            raise exc.HTTPBadRequest(explanation=unicode(exp))

        if values is None:
            msg = _("Not enough parameters to build a valid rule.")
            raise exc.HTTPBadRequest(explanation=msg)

        values['parent_group_id'] = security_group.id

        if self.security_group_api.rule_exists(security_group, values):
            msg = _('This rule already exists in group %s') % parent_group_id
            raise exc.HTTPBadRequest(explanation=msg)

        security_group_rule = self.security_group_api.add_rules(
                context, parent_group_id, security_group['name'], [values])[0]

        return {"security_group_rule": self._format_security_group_rule(
                                                        context,
                                                        security_group_rule)}

    def _rule_args_to_dict(self, context, to_port=None, from_port=None,
                           ip_protocol=None, cidr=None, group_id=None):

        if group_id is not None:
            group_id = self._validate_id(group_id)
            #check if groupId exists
            self.security_group_api.get(context, id=group_id)
            return self.security_group_api.new_group_ingress_rule(
                                    group_id, ip_protocol, from_port, to_port)
        else:
            cidr = self.security_group_api.parse_cidr(cidr)
            return self.security_group_api.new_cidr_ingress_rule(
                                        cidr, ip_protocol, from_port, to_port)

    def delete(self, req, id):
        context = self._authorize_context(req)

        id = self._validate_id(id)

        rule = self.security_group_api.get_rule(context, id)

        group_id = rule.parent_group_id

        security_group = self.security_group_api.get(context, None, group_id,
                                                     map_exception=True)

        self.security_group_api.remove_rules(context, security_group,
                                             [rule['id']])

        return webob.Response(status_int=202)


class ServerSecurityGroupController(SecurityGroupControllerBase):

    @wsgi.serializers(xml=SecurityGroupsTemplate)
    def index(self, req, server_id):
        """Returns a list of security groups for the given instance."""
        context = self._authorize_context(req)

        self.security_group_api.ensure_default(context)

        try:
            instance = self.compute_api.get(context, server_id)
        except exception.InstanceNotFound as exp:
            raise exc.HTTPNotFound(explanation=unicode(exp))

        groups = db.security_group_get_by_instance(context, instance['id'])

        result = [self._format_security_group(context, group)
                    for group in groups]

        return {'security_groups':
                list(sorted(result,
                            key=lambda k: (k['tenant_id'], k['name'])))}


class SecurityGroupActionController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(SecurityGroupActionController, self).__init__(*args, **kwargs)
        self.security_group_api = NativeSecurityGroupAPI()
        self.compute_api = compute.API(
                                   security_group_api=self.security_group_api)

    def _parse(self, body, action):
        try:
            body = body[action]
            group_name = body['name']
        except TypeError:
            msg = _("Missing parameter dict")
            raise webob.exc.HTTPBadRequest(explanation=msg)
        except KeyError:
            msg = _("Security group not specified")
            raise webob.exc.HTTPBadRequest(explanation=msg)

        if not group_name or group_name.strip() == '':
            msg = _("Security group name cannot be empty")
            raise webob.exc.HTTPBadRequest(explanation=msg)

        return group_name

    def _invoke(self, method, context, id, group_name):
        try:
            instance = self.compute_api.get(context, id)
            method(context, instance, group_name)
        except exception.SecurityGroupNotFound as exp:
            raise exc.HTTPNotFound(explanation=unicode(exp))
        except exception.InstanceNotFound as exp:
            raise exc.HTTPNotFound(explanation=unicode(exp))
        except exception.Invalid as exp:
            raise exc.HTTPBadRequest(explanation=unicode(exp))

        return webob.Response(status_int=202)

    @wsgi.action('addSecurityGroup')
    def _addSecurityGroup(self, req, id, body):
        context = req.environ['nova.context']
        authorize(context)

        group_name = self._parse(body, 'addSecurityGroup')

        return self._invoke(self.security_group_api.add_to_instance,
                            context, id, group_name)

    @wsgi.action('removeSecurityGroup')
    def _removeSecurityGroup(self, req, id, body):
        context = req.environ['nova.context']
        authorize(context)

        group_name = self._parse(body, 'removeSecurityGroup')

        return self._invoke(self.security_group_api.remove_from_instance,
                            context, id, group_name)


class Security_groups(extensions.ExtensionDescriptor):
    """Security group support"""

    name = "SecurityGroups"
    alias = "security_groups"
    namespace = "http://docs.openstack.org/compute/ext/securitygroups/api/v1.1"
    updated = "2011-07-21T00:00:00+00:00"

    def get_controller_extensions(self):
        controller = SecurityGroupActionController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        resources = []

        res = extensions.ResourceExtension('os-security-groups',
                                controller=SecurityGroupController())

        resources.append(res)

        res = extensions.ResourceExtension('os-security-group-rules',
                                controller=SecurityGroupRulesController())
        resources.append(res)

        res = extensions.ResourceExtension(
            'os-security-groups',
            controller=ServerSecurityGroupController(),
            parent=dict(member_name='server', collection_name='servers'))
        resources.append(res)

        return resources


class NativeSecurityGroupAPI(compute.api.SecurityGroupAPI):
    @staticmethod
    def raise_invalid_property(msg):
        raise exc.HTTPBadRequest(explanation=msg)

    @staticmethod
    def raise_group_already_exists(msg):
        raise exc.HTTPBadRequest(explanation=msg)

    @staticmethod
    def raise_invalid_group(msg):
        raise exc.HTTPBadRequest(explanation=msg)

    @staticmethod
    def raise_invalid_cidr(cidr, decoding_exception=None):
        raise exception.InvalidCidr(cidr=cidr)

    @staticmethod
    def raise_over_quota(msg):
        raise exc.HTTPBadRequest(explanation=msg)

    @staticmethod
    def raise_not_found(msg):
        raise exc.HTTPNotFound(explanation=msg)
