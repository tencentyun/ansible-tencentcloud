#!/usr/bin/env python

'''
TencentCloud CVM external inventory script
=================================

Generates inventory that Ansible can understand by making API request to
TencentCloud CVM.

NOTE: This script assumes Ansible is being executed where the environment
variables needed have already been set:
    export TENCENTCLOUD_SECRET_ID=xxxxx
    export TENCENTCLOUD_SECRET_KEY=xxxxx

This script also assumes there is an tencent_cloud.ini file alongside it.  To specify a
different path to tencent_cloud.ini, define the TENCENTCLOUD_INI_PATH environment variable:
    export TENCENTCLOUD_INI_PATH=/path/to/tencent_cloud.ini
'''

# Copyright (c) 2020, TencentCloud
#
#  This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible. If not, see http://www.gnu.org/licenses/.

import os
import argparse
import re
import configparser

from time import time
from tencentcloud.common import credential
from tencentcloud.cvm.v20170312 import cvm_client, models

try:
    import json
except ImportError:
    import simplejson as json


class CvmInventory(object):

    def _empty_inventory(self):
        return {"_meta": {"hostvars": {}}}

    def __init__(self):
        ''' Main execution path '''

        self.inventory = self._empty_inventory()

        # Index of hostname (address) to instance ID
        self.index = {}

        # TencentCloud credentials.
        self.credentials = {}

        # Init variables
        self.regions = []
        self.cache_path_cache = ""
        self.cache_path_index = ""
        self.cache_max_age = 0

        # Read settings and parse CLI arguments
        self.args = None
        self.parse_cli_args()
        self.read_settings()

        # Cache
        if self.args.refresh_cache:
            self.do_api_calls_update_cache()
        elif not self.is_cache_valid():
            self.do_api_calls_update_cache()

        # Data to print
        if self.args.host:
            data_to_print = self.get_host_info()
        elif self.args.list:
            if self.inventory == self._empty_inventory():
                data_to_print = self.get_inventory_from_cache()
            else:
                data_to_print = self.json_format_dict(self.inventory, True)

        print(data_to_print)

    def parse_cli_args(self):
        ''' Command line argument processing '''

        parser = argparse.ArgumentParser(
            description="Produce an Ansible Inventory file based on CVM")
        parser.add_argument('--list', action='store_true', default=True,
                            help='List instances (default: True)')
        parser.add_argument('--host', action='store',
                            help='Get all the variables about a specific instance')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                            help='Force refresh of cache by making API requests to CVM (default: False - use cache files)')
        self.args = parser.parse_args()

    def read_settings(self):
        ''' Reads the settings from the tencent_cloud.ini file '''

        config = configparser.ConfigParser()
        default_config_path = os.path.join(os.path.dirname(
            os.path.realpath(__file__)), "tencent_cloud.ini")
        config_path = os.path.expanduser(os.path.expandvars(
            os.environ.get("TENCENTCLOUD_INI_PATH", default_config_path)))
        config.read(config_path)

        # Credential
        secret_id = os.environ.get('TENCENTCLOUD_SECRET_ID', None)
        if not secret_id:
            secret_id = self.get_option(
                config, 'credentials', 'tencentcloud_secret_id')
        secret_key = os.environ.get('TENCENTCLOUD_SECRET_KEY')
        if not secret_key:
            secret_key = self.get_option(
                config, 'credentials', 'tencentcloud_secret_key')
        token = os.environ.get('TENCENTCLOUD_SECURITY_TOKEN', None)
        if not token:
            token = self.get_option(
                config, 'credentials', 'tencentcloud_security_token')
        self.credentials['tencentcloud_secret_id'] = secret_id
        self.credentials['tencentcloud_secret_key'] = secret_key
        self.credentials['tencentcloud_security_token'] = token

        # Regions
        config_regions = self.get_option(config, 'cvm', 'regions')
        exclude_regions = self.get_option(config, 'cvm', 'regions_exclude')
        if not config_regions or config_regions == 'all':
            all_regions = self.describe_regions()

            if exclude_regions:
                for region in all_regions:
                    if region in exclude_regions:
                        continue
            else:
                self.regions = all_regions
        else:
            self.regions = config_regions.split(",")

        # Cache
        cache_dir = os.path.expanduser(
            self.get_option(config, 'cvm', 'cache_path'))
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        cache_name = 'ansible-tencentcloud'
        self.cache_path_cache = cache_dir + "/%s.cache" % cache_name
        self.cache_path_index = cache_dir + "/%s.index" % cache_name
        self.cache_max_age = float(
            self.get_option(config, 'cvm', 'cache_max_age'))

        # Destination
        self.destination_variable = self.get_option(
            config, 'cvm', 'destination_variable')

        # Instance states
        cvm_valid_instance_states = [
            'PENDING', 'RUNNING', 'STOPPED', 'STARTING', 'STOPPING', 'REBOOTING']

        self.cvm_instance_states = []
        if self.get_option(config, 'cvm', 'all_instances'):
            self.cvm_instance_states.extend(cvm_valid_instance_states)
        elif self.get_option(config, 'cvm', 'instance_states'):
            for instance_state in self.get_option(config, 'cvm', 'instance_states').split(','):
                instance_state = instance_state.strip().upper()
                if instance_state not in cvm_valid_instance_states:
                    continue
                self.cvm_instance_states.append(instance_state)
        else:
            self.cvm_instance_states.append('RUNNING')

        # Configure nested groups instead of flat namespace.
        self.nested_groups = self.get_option(config, 'cvm', 'nested_groups')

        # Configure which groups should be created.
        group_by_options = [
            'group_by_instance_id',
            'group_by_region',
            'group_by_availability_zone',
            'group_by_instance_type',
            'group_by_image_id',
            'group_by_vpc_id',
            'group_by_subnet_id',
            'group_by_security_group',
            'group_by_tag_keys',
            'group_by_tag_none'
        ]
        for option in group_by_options:
            setattr(self, option, self.get_option(config, 'cvm', option))

        # Do we need to just include hosts that match a pattern?
        try:
            pattern_include = self.get_option(config, 'cvm', 'pattern_include')
            if pattern_include and len(pattern_include) > 0:
                self.pattern_include = re.compile(pattern_include)
            else:
                self.pattern_include = None
        except configparser.NoOptionError:
            self.pattern_include = None

        # Do we need to exclude hosts that match a pattern?
        try:
            pattern_exclude = self.get_option(config, 'cvm', 'pattern_exclude')
            if pattern_exclude and len(pattern_exclude) > 0:
                self.pattern_exclude = re.compile(pattern_exclude)
            else:
                self.pattern_exclude = None
        except configparser.NoOptionError:
            self.pattern_exclude = None

    def get_option(self, config, module, name, default=None):
        ''' Get module argument from config '''

        option = None
        if config.has_option(module, name):
            option = config.get(module, name)
        if option is None:
            return default
        return option

    def get_cvm_client(self, region):
        ''' create client to api server'''

        cred = credential.Credential(self.credentials['tencentcloud_secret_id'],
                                     self.credentials['tencentcloud_secret_key'],
                                     self.credentials['tencentcloud_security_token'])
        client = cvm_client.CvmClient(cred, region)
        return client

    def describe_regions(self):

        client = self.get_cvm_client('ap-shanghai')
        request = models.DescribeRegionsRequest()
        response = client.DescribeRegions(request)
        regions = []
        for item in response.RegionSet:
            regions.append(item.Region)
        return regions

    def do_api_calls_update_cache(self):
        ''' Do API calls to each region, and save data in cache files '''

        for region in self.regions:
            self.get_instances_by_region(region)

        self.write_to_cache(self.inventory, self.cache_path_cache)
        self.write_to_cache(self.index, self.cache_path_index)

    def get_instances_by_region(self, region):
        ''' Makes an API call to the list of instances in a particular region '''

        client = self.get_cvm_client(region)
        instances = []
        page_number = 0
        limit = 20
        while True:
            request = models.DescribeInstancesRequest()
            request.Offset = limit * page_number
            request.Limit = limit
            response = client.DescribeInstances(request)
            # print(request.to_json_string())
            # print(response.to_json_string())
            instances.extend(response.InstanceSet)
            if len(response.InstanceSet) < limit:
                break
            page_number += 1

        for instance in instances:
            self.add_instance(instance, region)

    def add_instance(self, instance, region):
        ''' Adds an instance to the inventory and index, as long as it is
        addressable '''

        # Only return instances with desired instance states
        if instance.InstanceState not in self.cvm_instance_states:
            return

        # Select the best destination address
        if self.destination_variable:
            dest = None
            if self.destination_variable == 'public_ip_address' and instance.PublicIpAddresses and len(instance.PublicIpAddresses) > 0:
                dest = instance.PublicIpAddresses[0]
            elif self.destination_variable == 'private_ip_address' and instance.PrivateIpAddresses and len(instance.PrivateIpAddresses) > 0:
                dest = instance.PrivateIpAddresses[0]

        if not dest:
            return

        # if we only want to include hosts that match a pattern, skip those that don't
        if self.pattern_include and not self.pattern_include.match(dest):
            return

        # if we need to exclude hosts that match a pattern, skip those
        if self.pattern_exclude and self.pattern_exclude.match(dest):
            return

        self.index[dest] = [region, instance.InstanceId]

        if self.group_by_instance_id:
            key = self.to_safe(instance.InstanceId)
            self.push(self.inventory, key, dest)
            if self.nested_groups:
                self.push_group(self.inventory, 'instances',
                                key)

        if self.group_by_region:
            key = self.to_safe(region)
            self.push(self.inventory, key, dest)
            if self.nested_groups:
                self.push_group(self.inventory, 'regions', key)

        if self.group_by_availability_zone:
            key = self.to_safe(instance.Placement.Zone)
            self.push(self.inventory, key, dest)
            if self.nested_groups:
                if self.group_by_region:
                    self.push_group(self.inventory, self.to_safe(region), key)
                self.push_group(self.inventory, 'zones', key)

        if self.group_by_image_id:
            key = self.to_safe(instance.ImageId)
            self.push(self.inventory, key, dest)
            if self.nested_groups:
                self.push_group(self.inventory, 'images', key)

        if self.group_by_instance_type:
            key = self.to_safe('type_' + instance.InstanceType)
            self.push(self.inventory, key, dest)
            if self.nested_groups:
                self.push_group(self.inventory, 'types', key)

        if self.group_by_vpc_id:
            key = self.to_safe('vpc_' + instance.VirtualPrivateCloud.VpcId)
            self.push(self.inventory, key, dest)
            if self.nested_groups:
                self.push_group(self.inventory, 'vpcs', key)

        if self.group_by_subnet_id:
            key = self.to_safe(
                'subnet_' + instance.VirtualPrivateCloud.SubnetId)
            self.push(self.inventory, key, dest)
            if self.nested_groups:
                self.push_group(self.inventory, 'subnets', key)

        if self.group_by_security_group:
            for group in instance.SecurityGroupIds:
                key = self.to_safe('security_group_' + group)
                self.push(self.inventory, key, dest)
                if self.nested_groups:
                    self.push_group(self.inventory, 'security_groups', key)

        if self.group_by_tag_keys:
            for tag in instance.Tags:
                if tag.Value:
                    key = self.to_safe('tag_' + tag.Key + '=' + tag.Value)
                else:
                    key = self.to_safe('tag_' + tag.Key)
                self.push(self.inventory, key, dest)
                if self.nested_groups:
                    self.push_group(self.inventory, 'tags',
                                    self.to_safe('tag_' + tag.Key))
                    self.push_group(self.inventory, self.to_safe(
                        'tag_' + tag.Key), key)

        if self.group_by_tag_none:
            self.push(self.inventory, 'tag_none', dest)
            if self.nested_groups:
                self.push_group(self.inventory, 'tags', 'tag_none')

        self.push(self.inventory, 'tencentcloud', dest)

        self.inventory['_meta']['hostvars'][dest] = self.get_host_info_dict_from_instance(
            instance)
        self.inventory['_meta']['hostvars'][dest]['ansible_ssh_host'] = dest

    def push(self, my_dict, key, element):
        ''' Push an element onto an array that may not have been defined in the dict '''

        group_info = my_dict.setdefault(key, [])
        if isinstance(group_info, dict):
            host_list = group_info.setdefault('hosts', [])
            host_list.append(element)
        else:
            group_info.append(element)

    def push_group(self, my_dict, key, element):
        ''' Push a group as a child of another group. '''

        parent_group = my_dict.setdefault(key, {})
        if not isinstance(parent_group, dict):
            parent_group = my_dict[key] = {'hosts': parent_group}
        child_groups = parent_group.setdefault('children', [])
        if element not in child_groups:
            child_groups.append(element)

    def write_to_cache(self, data, filename):
        ''' Writes data in JSON format to a file '''

        json_data = self.json_format_dict(data, True)
        cache = open(filename, 'w')
        cache.write(json_data)
        cache.close()

    def json_format_dict(self, data, pretty=False):
        ''' Converts a dict to a JSON object and dumps it as a formatted string '''

        if pretty:
            return json.dumps(data, sort_keys=True, indent=2)
        else:
            return json.dumps(data)

    def is_cache_valid(self):
        ''' Determines if the cache files have expired, or if it is still valid '''

        if os.path.isfile(self.cache_path_cache) and os.path.isfile(self.cache_path_index):
            update_time = os.path.getmtime(self.cache_path_cache)
            if (update_time + self.cache_max_age) > time():
                return True
        return False

    def get_inventory_from_cache(self):
        ''' Reads the inventory from the cache file and returns it as a JSON object '''

        cache = open(self.cache_path_cache, 'r')
        json_inventory = cache.read()
        return json_inventory

    def get_host_info(self):
        ''' Get variables about a specific host '''

        if len(self.index) == 0:
            self.load_index_from_cache()

        if self.args.host not in self.index:
            self.do_api_calls_update_cache()
            if self.args.host not in self.index:
                return self.json_format_dict({}, True)

        region, instance_id = self.index[self.args.host]

        instance = self.get_instance_by_id(region, instance_id)
        return self.json_format_dict(self.get_host_info_dict_from_instance(instance), True)

    def get_host_info_dict_from_instance(self, instance):
        ''' Get variables from instance that API response '''

        instance_vars = {}
        for key in vars(instance):
            if key == "InstanceId":
                instance_vars["id"] = instance.InstanceId
            elif key == "InstanceName":
                instance_vars["instance_name"] = instance.InstanceName
            elif key == "InstanceType":
                instance_vars["instance_type"] = instance.InstanceType
            elif key == "PublicIpAddresses":
                instance_vars["public_ip_address"] = instance.PublicIpAddresses
            elif key == "PrivateIpAddresses":
                instance_vars["private_ip_address"] = instance.PrivateIpAddresses
            elif key == "Placement":
                instance_vars["availability_zone"] = instance.Placement.Zone
            elif key == "InstanceState":
                instance_vars["status"] = instance.InstanceState
            else:
                pass

            # print('key: '+key)
            # print(value)

        return instance_vars

    def get_instance_by_id(self, region, instance_id):
        ''' Get CVM instance in a specified instance id '''

        client = self.get_cvm_client(region)
        request = models.DescribeInstancesRequest()
        request.InstanceIds = [instance_id]
        response = client.DescribeInstances(request)
        if len(response.InstanceSet) > 0:
            return response.InstanceSet[0]

    def load_index_from_cache(self):
        ''' Reads the index from the cache file sets self.index '''

        cache = open(self.cache_path_index, 'r')
        json_index = cache.read()
        self.index = json.loads(json_index)

    def to_safe(self, word):
        ''' Converts 'bad' characters in a string to underscores so they can be
        used as Ansible groups '''

        return re.sub(r"[^A-Za-z0-9\_]", "_", word)


if __name__ == '__main__':
    CvmInventory()
