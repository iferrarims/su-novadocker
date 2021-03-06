# Copyright (c) 2013 dotCloud, Inc.
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

"""
A Docker Hypervisor which allows running Linux Containers instead of VMs.
"""

import os
import socket
import time
import uuid

from oslo.config import cfg
from oslo.serialization import jsonutils
from oslo.utils import importutils
from oslo.utils import units

from nova.compute import flavors
from nova.compute import power_state
from nova.compute import task_states
from nova import exception
from nova.i18n import _
from nova.image import glance
from nova.openstack.common import fileutils
from nova.openstack.common import log
from nova.openstack.common import excutils
from nova.openstack.common import loopingcall
from nova import utils
from nova import utils as nova_utils
from nova import objects
from nova.virt import driver
from nova.virt import images
from novadocker.virt.docker import client as docker_client
from novadocker.virt.docker import hostinfo
from novadocker.virt.docker import host_monitor
from novadocker.virt.docker import cpuset_info
from novadocker.virt.docker import network
from novadocker.virt import hostutils
from docker import errors
from docker import utils as docker_utils

CONF = cfg.CONF
CONF.import_opt('my_ip', 'nova.netconf')
CONF.import_opt('instances_path', 'nova.compute.manager')

docker_opts = [
    cfg.StrOpt('host_url',
               default='unix:///var/run/docker.sock',
               help='tcp://host:port to bind/connect to or '
                    'unix://path/to/socket to use'),
    cfg.StrOpt('api_version',
               default='1.17',
               help='Docker API Version used to Manage Container. '),
    cfg.IntOpt('api_timeout',
               default=360,
               help='Docker API Timeout to finish a operation '),
    cfg.StrOpt('vif_driver',
               default='novadocker.virt.docker.vifs.DockerGenericVIFDriver'),
    cfg.StrOpt('snapshots_directory',
               default='$instances_path/snapshots',
               help='Location where docker driver will temporarily store '
                    'snapshots.'),
    cfg.StrOpt('dir_volume_path',
               default='/os_docker_volume',
               help='Location where container volume mounted.'),
    cfg.BoolOpt('privileged',
                default=True,
                help='Set true can own all root privileges in a container.'),
    cfg.StrOpt('docker_allocation_ratio',
               default=3),
    cfg.StrOpt('docker_cpu_mode',
               default='cpushare',
               help='Three mode support: cpushare(default)/cpuset/mix,'
                    'refer man of docker-run to definition of cpushare and cpuset '),
    cfg.StrOpt('docker_system_cpuset',
               default='-1',
               help='Location where obligate for system, default value is -1. '),
    cfg.StrOpt('docker_storage_type',
               default='device_mapper',
               help='Location where obligate for system, default value is -1. '
                    'Support list : device_mapper/overlayfs'),
    cfg.BoolOpt('delete_migration_source',
               default=False,
                help='Migration Source Node delete the tar from snapshot dir.')
]

CONF.register_opts(docker_opts, 'docker')

LOG = log.getLogger(__name__)


class DockerDriver(driver.ComputeDriver):
    """Docker hypervisor driver."""

    def __init__(self, virtapi):
        super(DockerDriver, self).__init__(virtapi)
        self._docker = None
        vif_class = importutils.import_class(CONF.docker.vif_driver)
        self.vif_driver = vif_class()

    @property
    def docker(self):
        if self._docker is None:
            self._docker = docker_client.DockerHTTPClient(CONF.docker.host_url,
                                                          api_version=CONF.docker.api_version,
                                                          api_timeout=CONF.docker.api_timeout)
        return self._docker

    def init_host(self, host):
        if self._is_daemon_running() is False:
            raise exception.NovaException(
                _('Docker daemon is not running or is not reachable'
                  ' (check the rights on /var/run/docker.sock)'))

    def _is_daemon_running(self):
        try:
            return self.docker.ping()
        except socket.error:
            return False

    def list_instances(self):
        res = []
        for container in self.docker.containers(all=True):
            res.append(container['Names'][0][1:])
        return res

    def _exist_container(self,container_name):
        for container in self.docker.containers(all=True):
            if container['Names'][0][1:] == container_name:
                return True

        return False

    def resize_container_disk(self, instance, disk_info):
        storage_type = CONF.docker.docker_storage_type
        flavor = flavors.extract_flavor(instance)

        if storage_type == "device_mapper":
            self._resize_dm_disk(disk_info)
            LOG.error('flavor type is %s' % type(flavor))
            #LOG.error('flavor content is %s' % flavor)
        elif storage_type == "overlayfs":
            self._resize_overlayfs_disk()
        else:
            LOG.info('Nova not support resize disk feature for %s .' % storage_type)

        return

    def _resize_dm_disk(self, disk_info):
        """resize container disk by device mapper command."""
        #msg = 'Disk "%s" does not exist, fetching it...'
        #LOG.debug(msg % image_meta['name'])
        pass

    def _resize_overlayfs_disk(self, disk_info):
        pass

    def attach_interface(self, instance, image_meta, vif):
        """Attach an interface to the container."""
        self.vif_driver.plug(instance, vif)
        container_id = self._get_container_id(instance)
        self.vif_driver.attach(instance, vif, container_id, sec_if=True)

    def detach_interface(self, instance, vif):
        """Detach an interface from the container."""
        self.vif_driver.unplug(instance, vif)

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        for vif in network_info:
            self.vif_driver.plug(instance, vif)

    def _attach_vifs(self, instance, network_info):
        """Plug VIFs into container."""
        if not network_info:
            return
        container_id = self._get_container_id(instance)
        if not container_id:
            LOG.warning('Container %s is not existed., attach vifs Failed.')
            return
        netns_path = '/var/run/netns'
        if not os.path.exists(netns_path):
            utils.execute(
                'mkdir', '-p', netns_path, run_as_root=True)
        nspid = self._find_container_pid(container_id)
        if not nspid:
            msg = _('Cannot find any PID under container "{0}"')
            raise RuntimeError(msg.format(container_id))
        netns_path = os.path.join(netns_path, container_id)
        utils.execute(
            'ln', '-sf', '/proc/{0}/ns/net'.format(nspid),
            '/var/run/netns/{0}'.format(container_id),
            run_as_root=True)
        utils.execute('ip', 'netns', 'exec', container_id, 'ip', 'link',
                      'set', 'lo', 'up', run_as_root=True)

        for vif in network_info:
            self.vif_driver.attach(instance, vif, container_id)

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        for vif in network_info:
            self.vif_driver.unplug(instance, vif)

    def _get_container_id(self, instance):
       return self._find_container_by_name(instance['name']).get('Id')

    def _find_container_by_name(self, name):
        try:
            containers = self.docker.containers(all=True, filters={'name': name})
            for ct in containers:
                if ct and ct['Names'][0][1:] == name:
                    return self.docker.inspect_container(ct['Id'])
        except errors.APIError as e:
            if e.response.status_code != 404:
                raise
        return {}

    def get_info(self, instance):
        container = self._find_container_by_name(instance['name'])
        if not container:
            raise exception.InstanceNotFound(instance_id=instance['name'])
        running = container['State'].get('Running')
        mem = container['Config'].get('Memory', 0)

        # NOTE(ewindisch): cgroups/lxc defaults to 1024 multiplier.
        #                  see: _get_cpu_shares for further explaination
        num_cpu = container['Config'].get('CpuShares', 0) / 1024

        # FIXME(ewindisch): Improve use of statistics:
        #                   For 'mem', we should expose memory.stat.rss, and
        #                   for cpu_time we should expose cpuacct.stat.system,
        #                   but these aren't yet exposed by Docker.
        #
        #                   Also see:
        #                    docker/docs/sources/articles/runmetrics.md
        info = {
            'max_mem': mem,
            'mem': mem,
            'num_cpu': num_cpu,
            'cpu_time': 0
        }
        info['state'] = (power_state.RUNNING if running
                         else power_state.SHUTDOWN)
        return info

    def get_host_stats(self, refresh=False):
        hostname = socket.gethostname()
        stats = self.get_available_resource(hostname)
        stats['host_hostname'] = stats['hypervisor_hostname']
        stats['host_name_label'] = stats['hypervisor_hostname']
        return stats

    def get_available_nodes(self, refresh=False):
        hostname = socket.gethostname()
        return [hostname]

    def get_available_resource(self, nodename):
        if not hasattr(self, '_nodename'):
            self._nodename = nodename
        if nodename != self._nodename:
            LOG.error(_('Hostname has changed from %(old)s to %(new)s. '
                        'A restart is required to take effect.'
                        ) % {'old': self._nodename,
                             'new': nodename})

        memory = hostinfo.get_memory_usage()
        docker_info = self.docker.info()
        disk = hostinfo.get_disk_usage(docker_info)
        vcpu_total = hostinfo.get_cpu_info() * int(CONF.docker.docker_allocation_ratio)

        stats = {
            'vcpus': int(vcpu_total),
            'vcpus_used': 0,
            'memory_mb': memory['total'] / units.Mi,
            'memory_mb_used': memory['used'] / units.Mi,
            'local_gb': disk['total'] / units.Gi,
            'local_gb_used': disk['used'] / units.Gi,
            'disk_available_least': disk['available'] / units.Gi,
            'hypervisor_type': 'docker',
            'hypervisor_version': utils.convert_version_to_int('1.0'),
            'hypervisor_hostname': self._nodename,
            'cpu_info': '?',
            'supported_instances': jsonutils.dumps([
                ('i686', 'docker', 'lxc'),
                ('x86_64', 'docker', 'lxc')
            ])
        }
        return stats

    def _find_container_pid(self, container_id):
        n = 0
        while True:
            # NOTE(samalba): We wait for the process to be spawned inside the
            # container in order to get the the "container pid". This is
            # usually really fast. To avoid race conditions on a slow
            # machine, we allow 10 seconds as a hard limit.
            if n > 15:
                return
            info = self.docker.inspect_container(container_id)
            if info:
                pid = info['State']['Pid']
                # Pid is equal to zero if it isn't assigned yet
                if pid:
                    return pid
            time.sleep(1)
            n += 1

    def _get_memory_limit_bytes(self, instance):
        if isinstance(instance, objects.Instance):
            return instance.get_flavor().memory_mb * units.Mi
        else:
            system_meta = utils.instance_sys_meta(instance)
            return int(system_meta.get(
                'instance_type_memory_mb', 0)) * units.Mi

    def _get_image_name(self, context, instance, image):
        fmt = image['container_format']
        if fmt != 'docker':
            msg = _('Image container format not supported ({0})')
            raise exception.InstanceDeployFailure(msg.format(fmt),
                                                  instance_id=instance['name'])
        return image['name']

    def _tag_image_name(self,image_meta, image_name):
        if (image_meta and image_meta.get('properties', {}).get('docker_image_type')):
            image_type = image_meta['properties'].get('docker_image_type')
        else:
            image_type = "repository"
        LOG.debug('docker_image_type is "%s".' % image_type)

        if image_type == "image":
            # image_meta_uuid= unicode(image_meta['id']).encode('utf-8')
            image_meta_uuid = image_meta['id']
            image_id = image_meta_uuid[0:8] + image_meta_uuid[9:13]
            self.docker.tag(image_id, repository=image_name)

    def _get_dir_volume(self, image_meta):
        ret = None
        log_volume = None
        data_volume= None
        other_volume=None

        if image_meta:
            if image_meta.get('properties', {}).get('log_volume'):
                log_volume = image_meta['properties'].get('log_volume')
            if image_meta.get('properties', {}).get('data_volume'):
                data_volume = image_meta['properties'].get('data_volume')
            if image_meta.get('properties', {}).get('other_volume'):
                other_volume = image_meta['properties'].get('other_volume')

        ret =  {"log_volume" : log_volume, "data_volume" : data_volume, "other_volume" : other_volume}
        return ret

    def _pull_missing_image(self, context, image_meta, instance):
        msg = 'Image name "%s" does not exist, fetching it...'
        LOG.debug(msg % image_meta['name'])

        # passing but that seems a bit complex right now.
        snapshot_directory = CONF.docker.snapshots_directory
        #if snapshot is not existed,create it.
        fileutils.ensure_tree(snapshot_directory)
        with utils.tempdir(dir=snapshot_directory) as tmpdir:
            try:
                out_path = os.path.join(tmpdir, uuid.uuid4().hex)

                images.fetch(context, image_meta['id'], out_path,
                             instance['user_id'], instance['project_id'])
                self.docker.load_repository_file(
                    self._encode_utf8(image_meta['name']),
                    out_path
                )
            except Exception as e:
                msg = _('Cannot load repository file: {0}')
                raise exception.NovaException(msg.format(e),
                                              instance_id=image_meta['name'])

        return self.docker.inspect_image(self._encode_utf8(image_meta['name']))

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):

        #get Image and image info.
        image_name = self._get_image_name(context, instance, image_meta)
        try:
            image_inspect_info = self.docker.inspect_image(image_name)
        except errors.APIError:
            image_inspect_info = None
        if not image_inspect_info:
            image_inspect_info = self._pull_missing_image(context, image_meta, instance)

        self._tag_image_name(image_meta, image_name)

        args = self._create_container_args(instance, image_meta, image_inspect_info, network_info, block_device_info)
        have_vol = self._create_volume_containers(instance, image_name, image_meta, network_info)
        if have_vol:
            vol_ct_name =  instance['name'] + '_vol'
            args['volumes_from'] = vol_ct_name

        container_id = self._create_container(instance, image_name, args)
        if not container_id:
            raise exception.InstanceDeployFailure(
                _('Cannot create container'),
                instance_id=instance['name'])

        #self.resize_container_disk(instance, "test")
        self._start_container(container_id, instance, network_info)

    def _create_container_args(self, instance, image_meta, image_inspect_info, network_info=None, block_device_info=None):
        args = {
            'hostname': instance['hostname'],
            'mem_limit': self._get_memory_limit_bytes(instance),
            'cpu_shares': self._get_cpu_shares(instance),
            'cpuset': self._get_cpu_set(instance),
            'network_mode': 'none',
            'privileged': True,
        }

        if not (image_inspect_info and image_inspect_info['Config']['Cmd']):
            args['command'] = 'sh -c "while true;do sleep 10;done"'
        # Glance command-line overrides any set in the Docker image
        if (image_meta and
                image_meta.get('properties', {}).get('os_command_line')):
            args['command'] = image_meta['properties'].get('os_command_line')

        #Workawound instance metadata.
        if 'metadata' in instance:
            pass
            #FIXME: it's not nice to set all metadata to container env.
            #args['environment'] = nova_utils.instance_meta(instance)

        dns_list = network.find_dns(network_info)
        if not dns_list:
            dns_list = None
        args['dns'] = dns_list

        return args

    def _create_volume_containers(self, instance, image_name, image_meta, network_info=None):
        dir_volumes = self._get_dir_volume(image_meta)
        log_volume = dir_volumes['log_volume']
        data_volume = dir_volumes['data_volume']
        other_volume = dir_volumes['other_volume']
        if not log_volume and not data_volume and not other_volume:
            return False

        nova_name = instance['name']
        first_ip = network.find_first_ip(instance, network_info)
        vol_ct_name = nova_name + '_vol'
        host_dir = CONF.docker.dir_volume_path
        all_volumes = []
        all_binds = []

        if log_volume:
            log_host_dir = host_dir + '/log/' + nova_name + '_' + first_ip
            log_bind = log_host_dir + ':' + log_volume
            all_volumes.append(log_volume)
            all_binds.append(log_bind)
        if data_volume:
            data_host_dir = host_dir + '/data/' + nova_name + '_' + first_ip
            data_bind = data_host_dir + ':' + data_volume
            all_volumes.append(data_volume)
            all_binds.append(data_bind)
        if other_volume:
            other_host_dir = host_dir + '/other/' + nova_name + '_' + first_ip
            other_bind = other_host_dir + ':' + other_volume
            all_volumes.append(other_volume)
            all_binds.append(other_bind)

        self.docker.create_container(image_name, name=vol_ct_name, network_disabled=True,volumes=all_volumes,
                                     host_config=self.docker.create_host_config(binds=all_binds))
        return True

    def _destroy_volume_container(self,instance, network_info=None):
        nova_name = instance['name']
        first_ip = network.find_first_ip(instance, network_info)
        vol_ct_name = nova_name + '_vol'
        if self._exist_container(vol_ct_name):
            self.docker.remove_container(vol_ct_name, force=True, v=True)
            host_dir = CONF.docker.dir_volume_path
            log_host_dir = host_dir + '/log/' + nova_name + '_' + first_ip
            data_host_dir = host_dir + '/data/' + nova_name + '_' + first_ip
            other_host_dir = host_dir + '/other/' + nova_name + '_' + first_ip

            if os.path.isdir(log_host_dir):
                __import__('shutil').rmtree(log_host_dir)
            if os.path.isdir(data_host_dir):
                __import__('shutil').rmtree(data_host_dir)
            if os.path.isdir(other_host_dir):
                __import__('shutil').rmtree(other_host_dir)

    def _create_container(self, instance, image_name, args):
        #args stack from spawn:   hostname/cpu_shares/cpuset/command/env
        #args maybe set in host_config:  privileged/mem_limit/network_mode/dns/volumes_from
        name = instance['name']
        hostname = args.pop('hostname', None)
        #mem_limit has been moved to host_config in API version 1.19
        #mem_limit = args.pop('mem_limit', None)
        cpu_shares = args.pop('cpu_shares', None)
        cpuset=args.pop('cpuset', None)
        environment = args.pop('environment', None)
        command = args.pop('command', None)
        host_config = docker_utils.create_host_config(**args)
        return self.docker.create_container(image_name,
                                            name=self._encode_utf8(name),
                                            hostname=hostname,
                                            cpu_shares=cpu_shares,
                                            cpuset=cpuset,
                                            environment=environment,
                                            command=command,
                                            host_config=host_config)

    def _start_container(self, container_id, instance, network_info=None):
        #get mem_list/network_mode/privileged/dns/cpuset/cpu_shares
        mem_limit = self._get_memory_limit_bytes(instance)
        network_mode = 'none'
        privileged = True
        dns_list = network.find_dns(network_info)
        if not dns_list:
            dns_list = None
        cpu_shares = self._get_cpu_shares(instance)
        cpuset = self._get_cpu_set(instance)

        volumes_from = None
        vol_ct_name =  instance['name'] + '_vol'
        if self._exist_container(vol_ct_name):
            volumes_from = vol_ct_name

        #self.docker.start(container_id)
        self.docker.update_start(container_id, mem_limit=mem_limit,
            network_mode=network_mode, privileged=privileged,
            dns=dns_list, cpu_shares=cpu_shares, cpuset=cpuset,
            volumes_from=volumes_from)

        if not network_info:
            return
        try:
            self.plug_vifs(instance, network_info)
            self._attach_vifs(instance, network_info)
        except Exception as e:
            LOG.warning(_('Cannot setup network: %s'),
                        e, instance=instance, exc_info=True)
            msg = _('Cannot setup network: {0}')
            self.docker.kill(container_id)
            # Why destroy container here!! it's Dangerous.
            #self.docker.remove_container(container_id, force=True)
            raise exception.InstanceDeployFailure(msg.format(e),
                                                  instance_id=instance['name'])

    def _stop_container(self, container_id, instance, timeout=5):
        try:
            self.docker.stop(container_id, max(timeout, 5))
        except errors.APIError as e:
            if 'Unpause the container before stopping' not in e.explanation:
                LOG.warning(_('Cannot stop container: %s'),
                            e, instance=instance, exc_info=True)
                raise
            self.docker.unpause(container_id)
            self.docker.stop(container_id, timeout)

    #destroy container network
    def _network_delete(self, instance, network_info, container_id):
        try:
            network.teardown_network(container_id)
            if network_info:
                self.unplug_vifs(instance, network_info)
        except Exception as e:
            LOG.warning(_('Cannot destroy the container network'
                          ' during reboot {0}').format(e),
                        exc_info=True)
            return

    #all of Nova Driver had this func, do not delete this.
    def cleanup(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True):
        """Cleanup after instance being destroyed by Hypervisor."""
        container_id = self._get_container_id(instance)
        if not container_id:
            self.unplug_vifs(instance, network_info)
            return
        self.docker.remove_container(container_id, force=True)
        self._network_delete(instance, network_info, container_id)

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True):
        container_id = self._get_container_id(instance)
        if not container_id:
            return
        self._stop_container(container_id, instance, 10)
        self.cleanup(context, instance, network_info,
                     block_device_info, destroy_disks)
        self._destroy_volume_container(instance, network_info)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        container_id = self._get_container_id(instance)
        if not container_id:
            return
        self._stop_container(container_id, instance, 10)
        self._network_delete(instance,network_info,container_id)
        self._start_container(container_id, instance, network_info)

    def power_on(self, context, instance, network_info, block_device_info):
        container_id = self._get_container_id(instance)
        if not container_id:
            return
        self._start_container(container_id, instance, network_info)

    def power_off(self, instance, timeout=0, retry_interval=0):
        container_id = self._get_container_id(instance)
        if not container_id:
            return
        self._stop_container(container_id, instance, 10)

    def pause(self, instance):
        """Pause the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        try:
            container_id = self._get_container_id(instance)
            self.docker.pause(container_id)
        except Exception as e:
            msg = _('Cannot pause container: {0}')
            raise exception.NovaException(msg.format(e),
                                          instance_id=instance['name'])

    def unpause(self, instance):
        """Unpause paused VM instance.

        :param instance: nova.objects.instance.Instance
        """
        try:
            container_id = self._get_container_id(instance)
            self.docker.unpause(container_id)
        except Exception as e:
            msg = _('Cannot unpause container: {0}')
            raise exception.NovaException(msg.format(e),
                                          instance_id=instance['name'])

    def restore(self, instance):
        container_id = self._get_container_id(instance)
        if not container_id:
            return

        self._start_container(container_id, instance)

    def get_console_output(self, context, instance):
        container_id = self._get_container_id(instance)
        if not container_id:
            return
        return self.docker.get_container_logs(container_id)

    def snapshot(self, context, instance, image_href, update_task_state):
        container_id = self._get_container_id(instance)
        if not container_id:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
        (image_service, image_id) = glance.get_remote_image_service(
            context, image_href)
        image = image_service.show(context, image_id, True)
        if ':' not in image['name']:
            commit_name = self._encode_utf8(image['name'])
            tag = 'latest'
        else:
            parts = self._encode_utf8(image['name']).rsplit(':', 1)
            commit_name = parts[0]
            tag = parts[1]

        self.docker.commit(container_id, repository=commit_name, tag=tag)

        update_task_state(task_state=task_states.IMAGE_UPLOADING,
                          expected_state=task_states.IMAGE_PENDING_UPLOAD)

        metadata = {
            'is_public': False,
            'status': 'active',
            'disk_format': 'raw',
            'container_format': 'docker',
            'name': image['name'],
            'properties': {
                'image_location': 'snapshot',
                'image_state': 'available',
                'status': 'available',
                'owner_id': instance['project_id'],
                'ramdisk_id': instance['ramdisk_id']
            }
        }
        if instance['os_type']:
            metadata['properties']['os_type'] = instance['os_type']

        try:
            raw = self.docker.get_image(commit_name)
            # Patch the seek/tell as urllib3 throws UnsupportedOperation
            raw.seek = lambda x=None, y=None: None
            raw.tell = lambda: None
            image_service.update(context, image_href, metadata, raw)
        except Exception as e:
            LOG.debug(_('Error saving image: %s'),
                      e, instance=instance, exc_info=True)
            msg = _('Error saving image: {0}')
            raise exception.NovaException(msg.format(e),
                                          instance_id=instance['name'])

    def _get_cpu_shares(self, instance):
        """Get allocated CPUs from configured flavor.

        Docker/lxc supports relative CPU allocation.

        cgroups specifies following:
         /sys/fs/cgroup/lxc/cpu.shares = 1024
         /sys/fs/cgroup/cpu.shares = 1024

        For that reason we use 1024 as multiplier.
        This multiplier allows to divide the CPU
        resources fair with containers started by
        the user (e.g. docker registry) which has
        the default CpuShares value of zero.
        """
        cpu_mode = CONF.docker.docker_cpu_mode
        if cpu_mode == 'cpuset' or cpu_mode == 'mix':
            flavor = flavors.extract_flavor(instance)
            return int(flavor['vcpus']) * 1024
        else:
            return

    def _get_cpu_set(self, instance):
        cpu_mode = CONF.docker.docker_cpu_mode
        system_cpuset = CONF.docker.docker_system_cpuset
        if cpu_mode == 'cpuset' or cpu_mode == 'mix':
            flavor = flavors.extract_flavor(instance)
            cpu_num = int(flavor['vcpus'])
            cpustats = cpuset_info.CpusetStatsMap(system_cpuset)
            cpustats.get_map()
            ori_list = cpustats.less_set_cpus(cpu_num)
            set_str = ori_list[0][3:]
            for cpu in ori_list[1:]:
                set_str = set_str  +  ',' +  cpu[3:]
            return set_str
        else:
            if system_cpuset != '-1':
                cpustats = cpuset_info.CpusetStatsMap(system_cpuset)
                ori_list = cpustats.get_unsystem_cpu()
                set_str = ori_list[0][3:]
                for cpu in ori_list[1:]:
                    set_str = set_str  +  ',' +  cpu[3:]
                return set_str
            else:
                return

    def get_host_uptime(self, host):
        return hostutils.sys_uptime()

    def get_monitor_info(self, host):
        # add by liuhaibin for get host information 2016-3-1
        """ get information
        :return: A dict
        """
        monitor_info = {}
        cpu_info = host_monitor.get_cpu_info()
        mem_info = host_monitor.get_mem_info()
        disk_info = host_monitor.get_disk_info()
        bios_info = host_monitor.get_bios_info()
        chassis_info = host_monitor.get_chassis_info()
        soft_info = host_monitor.get_software_info()
        monitor_info["cpu_info"] = cpu_info
        monitor_info["mem_info"] = mem_info
        monitor_info["disk_info"] = disk_info
        monitor_info["bios_info"] = bios_info
        monitor_info["chassis_info"] = chassis_info
        monitor_info["soft_info"] = soft_info
        return monitor_info

    def _encode_utf8(self, value):
        return unicode(value).encode('utf-8')

    def log_dict(self, d):
        for k,v in d.iteritems():
            LOG.debug("%s : %s" % (k,v))


    ##################################################################################
    #Migrate                                                                         #
    ##################################################################################
    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):
        LOG.debug("Starting migrate_disk_and_power_off",
                   instance=instance)
        container_id = self._get_container_id(instance)
        snapshot_directory = CONF.docker.snapshots_directory
        migrate_src = snapshot_directory + '/migrate_src/'
        migrate_dest = snapshot_directory + '/migrate_dest/'

        # Checks if the migration needs a disk resize down.
        for kind in ('root_gb', 'ephemeral_gb'):
            if flavor[kind] < instance[kind]:
                reason = _("Unable to resize disk down.")
                raise exception.InstanceFaultRollback(
                    exception.ResizeError(reason=reason))

        try:
            #commit to migrate_src
            utils.execute('mkdir', '-p', migrate_src)
            self.docker.commit(container = container_id, repository= instance['name'], tag='latest')
            image = self.docker.get_image(image=instance['name'])
            image_tar_name = migrate_src + instance['name']+ '.tar'
            image_tar = open(image_tar_name, 'w')
            image_tar.write(image.data)
            image_tar.close()

            #Stop the Container
            self.power_off(instance, timeout, retry_interval)


            hostutils.copy_image(migrate_src, migrate_dest, host=dest)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._cleanup_migration(dest, image_tar_name, image_name = instance['name'])

        return None


    def _cleanup_migration(self, migrate_src, image_name):
        """Used only for cleanup in case migrate_disk_and_power_off fails."""
        try:
            if os.path.exists(migrate_src):
                utils.execute('rm', '-rf', migrate_src)
            self.docker.remove_image(image_name)
        except Exception:
            pass

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        LOG.debug("Starting finish_migration", instance=instance)

        snapshot_directory = CONF.docker.snapshots_directory
        migrate_dest = snapshot_directory + '/migrate_dest/'
        image_name = instance['name']
        image_tar_name = migrate_dest + image_name + '.tar'

        #get Image and image info
        self.docker.load_repository_file(
                    image_name,
                    image_tar_name
                )
        image_inspect_info = self.docker.inspect_image(image_name)

        args = self._create_container_args(instance, image_meta, image_inspect_info, network_info, block_device_info)
        container_id = self._create_container(instance, image_name, args)
        if not container_id:
            raise exception.InstanceDeployFailure(
                _('Cannot create container'),
                instance_id=instance['name'])

        #self.resize_container_disk(instance, "test")
        self._start_container(container_id, instance, network_info)
        utils.execute('rm', '-rf', image_tar_name, delay_on_retry=True,
                          attempts=5)

    def confirm_migration(self, migration, instance, network_info):
        """Confirms a resize, destroying the source VM."""
        self._cleanup_resize(instance, network_info)

    def _cleanup_resize(self, instance, network_info):
        # NOTE(wangpan): we get the pre-grizzly instance path firstly,
        #                so the backup dir of pre-grizzly instance can
        #                be deleted correctly with grizzly or later nova.
        container_id = self._get_container_id(instance)
        if not container_id:
            return
        self._stop_container(container_id, instance, 10)
        self.docker.remove_container(container_id, force=True)
        self._network_delete(instance, network_info, container_id)


        delete_migration_source = CONF.docker.delete_migration_source
        if not delete_migration_source:
            return
        snapshot_directory = CONF.docker.snapshots_directory
        migrate_src = snapshot_directory + '/migrate_src/'
        image_name = instance['name']
        image_tar_name = migrate_src + image_name + '.tar'
        utils.execute('rm', '-rf', image_tar_name, delay_on_retry=True,
                          attempts=5)


    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        LOG.debug("Starting finish_revert_migration",
                  instance=instance)

        self.power_on(None, instance, network_info, None)

    @staticmethod
    def get_host_ip_addr():
        return CONF.my_ip

    def get_volume_connector(self, instance):
        pass


class ContainerUtils(object):
    """ tools for container """
    def __init__(self):
        self._docker = None

    @property
    def docker(self):
        if self._docker is None:
            self._docker = docker_client.DockerHTTPClient(CONF.docker.host_url,
                                                          api_version=CONF.docker.api_version,
                                                          api_timeout=CONF.docker.api_timeout)
        return self._docker

    def get_container_id(self, instance):
       return self.find_container_by_name(instance['name']).get('Id')

    def find_container_by_name(self, name):
        try:
            containers = self.docker.containers(all=True, filters={'name': name})
            for ct in containers:
                if ct and ct['Names'][0][1:] == name:
                    return self.docker.inspect_container(ct['Id'])
        except errors.APIError as e:
            if e.response.status_code != 404:
                raise
        return {}

    def container_is_running(self, instance):
        name = instance['name']
        try:
            containers = self.docker.containers(all=True, filters={'name': name})
            for ct in containers:
                if ct and ct['Names'][0][1:] == name:
                    if 'Up' in ct['Status']:
                        return True
                    else:
                        return False
        except errors.APIError as e:
            if e.response.status_code != 404:
                raise