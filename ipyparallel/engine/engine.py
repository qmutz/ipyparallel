"""A simple engine that talks to a controller over 0MQ.
it handles registration, etc. and launches a kernel
connected to the Controller's Schedulers.
"""
# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
from __future__ import print_function

import sys
import time
from getpass import getpass

import zmq
from ipykernel.kernelapp import IPKernelApp
from ipython_genutils.py3compat import cast_bytes
from jupyter_client.localinterfaces import localhost
from traitlets import Bool
from traitlets import CBytes
from traitlets import default
from traitlets import Dict
from traitlets import Float
from traitlets import Instance
from traitlets import Integer
from traitlets import observe
from traitlets import Type
from traitlets import Unicode
from zmq.eventloop import zmqstream

from .kernel import IPythonParallelKernel as Kernel
from ipyparallel.controller.heartmonitor import Heart
from ipyparallel.factory import RegistrationFactory
from ipyparallel.util import disambiguate_url
from ipyparallel.util import ioloop

DEFAULT_MPI_INIT = """
from mpi4py import MPI
mpi_rank = MPI.COMM_WORLD.Get_rank()
mpi_size = MPI.COMM_WORLD.Get_size()
"""


class EngineFactory(RegistrationFactory):
    """IPython engine"""

    # configurables:
    out_stream_factory = Type(
        'ipykernel.iostream.OutStream',
        config=True,
        help="""The OutStream for handling stdout/err.
        Typically 'ipykernel.iostream.OutStream'""",
    )
    display_hook_factory = Type(
        'ipykernel.displayhook.ZMQDisplayHook',
        config=True,
        help="""The class for handling displayhook.
        Typically 'ipykernel.displayhook.ZMQDisplayHook'""",
    )
    location = Unicode(
        config=True,
        help="""The location (an IP address) of the controller.  This is
        used for disambiguating URLs, to determine whether
        loopback should be used to connect or the public address.""",
    )
    timeout = Float(
        5.0,
        config=True,
        help="""The time (in seconds) to wait for the Controller to respond
        to registration requests before giving up.""",
    )
    max_heartbeat_misses = Integer(
        50,
        config=True,
        help="""The maximum number of times a check for the heartbeat ping of a 
        controller can be missed before shutting down the engine.
        
        If set to 0, the check is disabled.""",
    )
    sshserver = Unicode(
        config=True,
        help="""The SSH server to use for tunneling connections to the Controller.""",
    )
    sshkey = Unicode(
        config=True,
        help="""The SSH private key file to use when tunneling connections to the Controller.""",
    )
    paramiko = Bool(
        sys.platform == 'win32',
        config=True,
        help="""Whether to use paramiko instead of openssh for tunnels.""",
    )

    use_mpi = Bool(
        False,
        config=True,
        help="""Enable MPI integration.
        
        If set, MPI rank will be requested for my rank,
        and additionally `mpi_init` will be executed in the interactive shell.
        """,
    )
    init_mpi = Unicode(
        DEFAULT_MPI_INIT,
        config=True,
        help="""Code to execute in the user namespace when initializing MPI""",
    )

    @property
    def tunnel_mod(self):
        from zmq.ssh import tunnel

        return tunnel

    # not configurable:
    connection_info = Dict()
    user_ns = Dict()
    id = Integer(
        None,
        allow_none=True,
        config=True,
        help="""Request this engine ID.
        
        If run in MPI, will use the MPI rank.
        Otherwise, let the Hub decide what our rank should be.
        """,
    )

    @default('id')
    def _id_default(self):
        if not self.use_mpi:
            return None
        from mpi4py import MPI

        if MPI.COMM_WORLD.size > 1:
            self.log.debug("MPI rank = %i", MPI.COMM_WORLD.rank)
            return MPI.COMM_WORLD.rank

    registrar = Instance('zmq.eventloop.zmqstream.ZMQStream', allow_none=True)
    kernel = Instance(Kernel, allow_none=True)
    hb_check_period = Integer()

    # States for the heartbeat monitoring
    # Initial values for monitored and pinged must satisfy "monitored > pinged == False" so that
    # during the first check no "missed" ping is reported. Must be floats for Python 3 compatibility.
    _hb_last_pinged = 0.0
    _hb_last_monitored = 0.0
    _hb_missed_beats = 0
    # The zmq Stream which receives the pings from the Heart
    _hb_listener = None

    bident = CBytes()
    ident = Unicode()

    @observe('ident')
    def _ident_changed(self, change):
        self.bident = cast_bytes(change['new'])

    using_ssh = Bool(False)

    def __init__(self, **kwargs):
        super(EngineFactory, self).__init__(**kwargs)
        self.ident = self.session.session

    def init_connector(self):
        """construct connection function, which handles tunnels."""
        self.using_ssh = bool(self.sshkey or self.sshserver)

        if self.sshkey and not self.sshserver:
            # We are using ssh directly to the controller, tunneling localhost to localhost
            self.sshserver = self.url.split('://')[1].split(':')[0]

        if self.using_ssh:
            if self.tunnel_mod.try_passwordless_ssh(
                self.sshserver, self.sshkey, self.paramiko
            ):
                password = False
            else:
                password = getpass("SSH Password for %s: " % self.sshserver)
        else:
            password = False

        def connect(s, url):
            url = disambiguate_url(url, self.location)
            if self.using_ssh:
                self.log.debug("Tunneling connection to %s via %s", url, self.sshserver)
                return self.tunnel_mod.tunnel_connection(
                    s,
                    url,
                    self.sshserver,
                    keyfile=self.sshkey,
                    paramiko=self.paramiko,
                    password=password,
                )
            else:
                return s.connect(url)

        def maybe_tunnel(url):
            """like connect, but don't complete the connection (for use by heartbeat)"""
            url = disambiguate_url(url, self.location)
            if self.using_ssh:
                self.log.debug("Tunneling connection to %s via %s", url, self.sshserver)
                url, tunnelobj = self.tunnel_mod.open_tunnel(
                    url,
                    self.sshserver,
                    keyfile=self.sshkey,
                    paramiko=self.paramiko,
                    password=password,
                )
            return str(url)

        return connect, maybe_tunnel

    def register(self):
        """send the registration_request"""

        self.log.info("Registering with controller at %s" % self.url)
        ctx = self.context
        connect, maybe_tunnel = self.init_connector()
        reg = ctx.socket(zmq.DEALER)
        reg.setsockopt(zmq.IDENTITY, self.bident)
        connect(reg, self.url)
        self.registrar = zmqstream.ZMQStream(reg, self.loop)

        content = dict(uuid=self.ident)
        if self.id is not None:
            self.log.info("Requesting id: %i", self.id)
            content['id'] = self.id
        self.registrar.on_recv(
            lambda msg: self.complete_registration(msg, connect, maybe_tunnel)
        )
        # print (self.session.key)

        self.session.send(self.registrar, "registration_request", content=content)

    def _report_ping(self, msg):
        """Callback for when the heartmonitor.Heart receives a ping"""
        # self.log.debug("Received a ping: %s", msg)
        self._hb_last_pinged = time.time()

    def complete_registration(self, msg, connect, maybe_tunnel):
        # print msg
        self.loop.remove_timeout(self._abort_timeout)
        ctx = self.context
        loop = self.loop
        identity = self.bident
        idents, msg = self.session.feed_identities(msg)
        msg = self.session.deserialize(msg)
        content = msg['content']
        info = self.connection_info

        def url(key):
            """get zmq url for given channel"""
            return str(info["interface"] + ":%i" % info[key])

        def urls(key):
            return [f'{info["interface"]}:{port}' for port in info[key]]

        if content['status'] == 'ok':
            if self.id is not None and content['id'] != self.id:
                self.log.warning(
                    "Did not get the requested id: %i != %i", content['id'], self.id
                )
            self.id = content['id']

            # launch heartbeat
            # possibly forward hb ports with tunnels
            hb_ping = maybe_tunnel(url('hb_ping'))
            hb_pong = maybe_tunnel(url('hb_pong'))

            hb_monitor = None
            if self.max_heartbeat_misses > 0:
                # Add a monitor socket which will record the last time a ping was seen
                mon = self.context.socket(zmq.SUB)
                mport = mon.bind_to_random_port('tcp://%s' % localhost())
                mon.setsockopt(zmq.SUBSCRIBE, b"")
                self._hb_listener = zmqstream.ZMQStream(mon, self.loop)
                self._hb_listener.on_recv(self._report_ping)

                hb_monitor = "tcp://%s:%i" % (localhost(), mport)

            heart = Heart(hb_ping, hb_pong, hb_monitor, heart_id=identity)
            heart.start()

            # create Shell Connections (MUX, Task, etc.):
            shell_addrs = [url('mux'), url('task')] + urls('broadcast')

            self.log.info(f'ENGINE: shell_addrs: {shell_addrs}')

            # Use only one shell stream for mux and tasks
            stream = zmqstream.ZMQStream(ctx.socket(zmq.ROUTER), loop)
            stream.setsockopt(zmq.IDENTITY, identity)
            self.log.debug("Setting shell identity %r", identity)

            shell_streams = [stream]
            for addr in shell_addrs:
                self.log.info("Connecting shell to %s", addr)
                connect(stream, addr)

            # control stream:
            control_addr = url('control')
            control_stream = zmqstream.ZMQStream(ctx.socket(zmq.ROUTER), loop)
            control_stream.setsockopt(zmq.IDENTITY, identity)
            connect(control_stream, control_addr)

            # create iopub stream:
            iopub_addr = url('iopub')
            iopub_socket = ctx.socket(zmq.PUB)
            iopub_socket.setsockopt(zmq.IDENTITY, identity)
            connect(iopub_socket, iopub_addr)
            try:
                from ipykernel.iostream import IOPubThread
            except ImportError:
                pass
            else:
                iopub_socket = IOPubThread(iopub_socket)
                iopub_socket.start()

            # disable history:
            self.config.HistoryManager.hist_file = ':memory:'

            # Redirect input streams and set a display hook.
            if self.out_stream_factory:
                sys.stdout = self.out_stream_factory(
                    self.session, iopub_socket, u'stdout'
                )
                sys.stdout.topic = cast_bytes('engine.%i.stdout' % self.id)
                sys.stderr = self.out_stream_factory(
                    self.session, iopub_socket, u'stderr'
                )
                sys.stderr.topic = cast_bytes('engine.%i.stderr' % self.id)
            if self.display_hook_factory:
                sys.displayhook = self.display_hook_factory(self.session, iopub_socket)
                sys.displayhook.topic = cast_bytes('engine.%i.execute_result' % self.id)

            self.kernel = Kernel(
                parent=self,
                engine_id=self.id,
                ident=self.ident,
                session=self.session,
                control_stream=control_stream,
                shell_streams=shell_streams,
                iopub_socket=iopub_socket,
                loop=loop,
                user_ns=self.user_ns,
                log=self.log,
            )

            self.kernel.shell.display_pub.topic = cast_bytes(
                'engine.%i.displaypub' % self.id
            )

            # periodically check the heartbeat pings of the controller
            # Should be started here and not in "start()" so that the right period can be taken
            # from the hubs HeartBeatMonitor.period
            if self.max_heartbeat_misses > 0:
                # Use a slightly bigger check period than the hub signal period to not warn unnecessary
                self.hb_check_period = int(content['hb_period']) + 10
                self.log.info(
                    "Starting to monitor the heartbeat signal from the hub every %i ms.",
                    self.hb_check_period,
                )
                self._hb_reporter = ioloop.PeriodicCallback(
                    self._hb_monitor, self.hb_check_period
                )
                self._hb_reporter.start()
            else:
                self.log.info(
                    "Monitoring of the heartbeat signal from the hub is not enabled."
                )

            # FIXME: This is a hack until IPKernelApp and IPEngineApp can be fully merged
            app = IPKernelApp(
                parent=self, shell=self.kernel.shell, kernel=self.kernel, log=self.log
            )
            if self.use_mpi and self.init_mpi:
                app.exec_lines.insert(0, self.init_mpi)
            app.init_profile_dir()
            app.init_code()

            self.kernel.start()
        else:
            self.log.fatal("Registration Failed: %s" % msg)
            raise Exception("Registration Failed: %s" % msg)

        self.log.info("Completed registration with id %i" % self.id)

    def abort(self):
        self.log.fatal("Registration timed out after %.1f seconds" % self.timeout)
        if self.url.startswith('127.'):
            self.log.fatal(
                """
            If the controller and engines are not on the same machine,
            you will have to instruct the controller to listen on an external IP (in ipcontroller_config.py):
                c.HubFactory.ip='*' # for all interfaces, internal and external
                c.HubFactory.ip='192.168.1.101' # or any interface that the engines can see
            or tunnel connections via ssh.
            """
            )
        self.session.send(
            self.registrar, "unregistration_request", content=dict(id=self.id)
        )
        time.sleep(1)
        sys.exit(255)

    def _hb_monitor(self):
        """Callback to monitor the heartbeat from the controller"""
        self._hb_listener.flush()
        if self._hb_last_monitored > self._hb_last_pinged:
            self._hb_missed_beats += 1
            self.log.warn(
                "No heartbeat in the last %s ms (%s time(s) in a row).",
                self.hb_check_period,
                self._hb_missed_beats,
            )
        else:
            # self.log.debug("Heartbeat received (after missing %s beats).", self._hb_missed_beats)
            self._hb_missed_beats = 0

        if self._hb_missed_beats >= self.max_heartbeat_misses:
            self.log.fatal(
                "Maximum number of heartbeats misses reached (%s times %s ms), shutting down.",
                self.max_heartbeat_misses,
                self.hb_check_period,
            )
            self.session.send(
                self.registrar, "unregistration_request", content=dict(id=self.id)
            )
            self.loop.stop()

        self._hb_last_monitored = time.time()

    def start(self):
        loop = self.loop

        def _start():
            self.register()
            self._abort_timeout = loop.add_timeout(
                loop.time() + self.timeout, self.abort
            )

        self.loop.add_callback(_start)
