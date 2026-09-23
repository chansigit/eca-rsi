"""Single-owner Temporal Server and PostgreSQL on durable shared storage.

The filesystem must provide coherent cross-host flock, rename and fsync. This
does not acquire Slurm resources. PostgreSQL and every service child inherit
the ownership lock, so losing the supervisor alone cannot authorize a second
database writer. Heartbeat expiry never transfers database ownership.
"""
import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid

from ..warm_pool.backend import parent_death_signal
from ..warm_pool.state import file_digest, lock, read, save


def endpoint(root):
    record = read(Path(root) / 'service.json', {})
    age = time.time() - record.get('observed_at', 0)
    if record.get('state') != 'ready' or not -5 <= age <= 30:
        raise ConnectionError('Temporal service is unavailable; waiting for its owner to recover')
    return record


def server_config(address, port, database_port, user, password):
    stores = {}
    for name in ('temporal', 'temporal_visibility'):
        stores[name] = {'sql': dict(pluginName='postgres12', databaseName=name,
            connectAddr=f'127.0.0.1:{database_port}', connectProtocol='tcp',
            user=user, password=password, maxConns=10, maxIdleConns=2, maxConnLifetime='1h')}
    services = {name: {'rpc': dict(grpcPort=port + offset, membershipPort=port + 100 + offset,
                                 bindOnIP=address)}
                for name, offset in (('frontend', 0), ('history', 1), ('matching', 2), ('worker', 6))}
    return dict(log=dict(stdout=True, level='warn'),
        persistence=dict(defaultStore='temporal', visibilityStore='temporal_visibility',
                         numHistoryShards=4, datastores=stores),
        services=services,
        clusterMetadata=dict(enableGlobalNamespace=False, failoverVersionIncrement=10,
            masterClusterName='active', currentClusterName='active',
            clusterInformation={'active': dict(enabled=True, initialFailoverVersion=1,
                rpcName='frontend', rpcAddress=f'{address}:{port}')}))


async def serve(args):
    from google.protobuf.duration_pb2 import Duration
    from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
    from temporalio.client import Client
    from temporalio.service import RPCError, RPCStatusCode

    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('Temporal state directory must be owned by you with mode 0700')
    address = socket.gethostbyname(args.bind)
    if address == '0.0.0.0':
        raise ValueError('Use a specific interface address, or 127.0.0.1 for local use')
    ports = [args.port + n for n in (0, 1, 2, 6, 100, 101, 102, 106)] + [args.database_port]
    if args.ui_port:
        ports.append(args.ui_port)
    if len(set(ports)) != len(ports) or any(not 1024 <= n <= 65535 for n in ports):
        raise ValueError('Service ports must be distinct unprivileged ports')
    binaries = [args.postgres_bin / n for n in ('postgres', 'initdb', 'psql', 'createdb')]
    binaries += [args.temporal_dir / n for n in ('temporal-server', 'temporal-sql-tool')]
    if args.ui_port:
        binaries.append(args.temporal_dir / 'ui-server')
    if any(not p.is_absolute() or not os.access(p, os.X_OK) for p in binaries):
        raise ValueError('Explicit installed PostgreSQL and Temporal binaries are required')
    if not args.schema_dir.is_absolute() or not (args.schema_dir / 'temporal/versioned').is_dir():
        raise ValueError('Use matching Temporal release schema/postgresql/v12 directory')
    runtime = {str(p): file_digest(p) for p in binaries}
    runtime.update({str(p): file_digest(p) for p in sorted(args.schema_dir.rglob('*')) if p.is_file()})
    env = dict(os.environ, LC_ALL='C', LANG='C', GOMAXPROCS='2')
    # Never allow inherited database credentials/settings to select another store.
    env = {k: v for k, v in env.items() if not k.startswith(('PG', 'SQL_', 'TEMPORAL_'))}
    os.umask(0o077)
    with lock(root / 'owner.lock', blocking=False) as ownership:
        installed = read(root / 'runtime.json')
        if installed is not None and installed != runtime:
            raise ValueError('Runtime changed; explicit offline database upgrade is required')
        save(root / 'runtime.json', runtime)
        password_file = root / 'database-password'
        if not password_file.exists():
            if (root / 'postgres').exists():
                raise ValueError('Existing database is missing its credential; refusing to replace it')
            password_file.write_text(secrets.token_urlsafe(32) + '\n')
            with password_file.open('r') as stream:
                os.fsync(stream.fileno())
        password = password_file.read_text().strip()
        user = getpass.getuser()
        env.update(PGUSER=user, PGPASSWORD=password, SQL_PASSWORD=password)
        children = []
        record = dict(generation=uuid.uuid4().hex, host=socket.gethostname(), pid=os.getpid(),
                      endpoint=f'{address}:{args.port}', ui_port=args.ui_port,
                      started_at=time.time(), persistence='postgresql', root=str(root))
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)

        def publish(state):
            save(root / 'service.json', dict(record, state=state, observed_at=time.time()))

        def run(command):
            with (root / 'setup.log').open('a') as log:
                subprocess.run([str(v) for v in command], env=env, check=True,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=120,
                               pass_fds=(ownership.fileno(),), preexec_fn=parent_death_signal(os.getpid()))

        def start(command, name):
            with (root / (name + '.log')).open('a') as log:
                process = subprocess.Popen([str(v) for v in command], env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    pass_fds=(ownership.fileno(),), preexec_fn=parent_death_signal(os.getpid()))
            children.append((name, process))
            return process

        def check_children():
            for name, process in children:
                if process.poll() is not None:
                    if name == 'ui':
                        record['ui_state'] = 'failed'
                        continue  # A read-only UI failure cannot take down workflow persistence.
                    raise RuntimeError(f'{name} exited with {process.returncode}; see {root / (name + ".log")}')
            if stopped.is_set():
                raise InterruptedError('Service stopping')

        publish('starting')
        sockets = None
        try:
            database = root / 'postgres'
            if not (database / 'PG_VERSION').exists():
                run([args.postgres_bin / 'initdb', '-D', database, '--encoding=UTF8', '--locale=C',
                     '--auth-local=peer', '--auth-host=scram-sha-256', '--pwfile', password_file])
            # Socket files are disposable; WAL and data stay entirely under root.
            sockets = tempfile.mkdtemp(prefix='rsi-pg-')
            start([args.postgres_bin / 'postgres', '-D', database, '-k', sockets,
                '-p', args.database_port, '-h', '127.0.0.1',
                '-c', 'fsync=on', '-c', 'synchronous_commit=on', '-c', 'full_page_writes=on',
                '-c', 'shared_buffers=64MB', '-c', 'max_connections=100'], 'postgres')
            sql = [args.postgres_bin / 'psql', '-h', '127.0.0.1', '-p', str(args.database_port),
                   '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1']
            def query(db, statement):
                return subprocess.run([str(v) for v in sql] + ['-d', db, '-c', statement],
                    env=env, capture_output=True, text=True, timeout=5)
            deadline = time.monotonic() + 90
            while query('postgres', 'SELECT 1').returncode:
                check_children()
                if time.monotonic() > deadline:
                    raise TimeoutError('PostgreSQL startup timed out')
                await asyncio.sleep(1)
            for name, schema in (('temporal', 'temporal'), ('temporal_visibility', 'visibility')):
                exists = query('postgres', f"SELECT 1 FROM pg_database WHERE datname='{name}'")
                exists.check_returncode()
                if exists.stdout.strip() != '1':
                    run([args.postgres_bin / 'createdb', '-h', '127.0.0.1', '-p', args.database_port, name])
                tool = [args.temporal_dir / 'temporal-sql-tool', '--plugin', 'postgres12',
                        '--endpoint', '127.0.0.1', '--port', args.database_port,
                        '--user', user, '--database', name]
                version = query(name, "SELECT to_regclass('schema_version')")
                version.check_returncode()
                if not version.stdout.strip():
                    run(tool + ['setup-schema', '-v', '0.0'])
                run(tool + ['update-schema', '-d', args.schema_dir / schema / 'versioned'])
            config = server_config(address, args.port, args.database_port, user, password)
            config['global'] = {'membership': {'maxJoinDuration': '30s', 'broadcastAddress': address}}
            if args.dynamic_config:
                # Hot-reloaded server knobs (workflow task timeout, history limits) live with the
                # deployment, not in this file; the path is only wired in when one is given.
                config['dynamicConfigClient'] = dict(filepath=str(args.dynamic_config.resolve()), pollInterval='60s')
            save(root / 'server.yaml', config)  # JSON is valid YAML; no templating dependency.
            start([args.temporal_dir / 'temporal-server', '--config-file', root / 'server.yaml',
                   '--allow-no-auth', 'start'], 'temporal')
            deadline = time.monotonic() + 120
            while True:
                check_children()
                try:
                    client = await asyncio.wait_for(Client.connect(record['endpoint']), 5)
                    if await asyncio.wait_for(client.service_client.check_health(), 5):
                        break
                except (RuntimeError, TimeoutError, RPCError):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError('Temporal startup timed out')
                await asyncio.sleep(1)
            try:
                await client.workflow_service.register_namespace(RegisterNamespaceRequest(
                    namespace='default', workflow_execution_retention_period=Duration(seconds=30 * 86400)))
            except RPCError as exc:
                if exc.status != RPCStatusCode.ALREADY_EXISTS:
                    raise
            if args.ui_port:
                ui = root / 'ui'
                ui.mkdir(exist_ok=True)
                save(ui / 'development.yaml', dict(temporalGrpcAddress=record['endpoint'],
                    host=address, port=args.ui_port, enableUi=True, defaultNamespace='default',
                    disableWriteActions=True, disableNewsFetch=True,
                    cors={'allowOrigins': [f'http://localhost:{args.ui_port}', f'http://127.0.0.1:{args.ui_port}']}))
                start([args.temporal_dir / 'ui-server', '--root', root, '--config', 'ui', 'start'], 'ui')
            print(json.dumps(record), flush=True)
            while not stopped.is_set():
                check_children()
                publish('ready')
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=5)
                except TimeoutError:
                    pass
        finally:
            publish('stopping')
            for name, process in reversed(children):
                if process.poll() is None:
                    process.send_signal(signal.SIGINT if name == 'postgres' else signal.SIGTERM)
                    try:
                        await asyncio.to_thread(process.wait, timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait)
            if sockets is not None:
                shutil.rmtree(sockets, ignore_errors=True)
            publish('stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--postgres-bin', type=Path, required=True)
    parser.add_argument('--temporal-dir', type=Path, required=True)
    parser.add_argument('--schema-dir', type=Path, required=True)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7333)
    parser.add_argument('--database-port', type=int, default=55432)
    parser.add_argument('--ui-port', type=int, default=8333)
    parser.add_argument('--dynamic-config', type=Path, help='Temporal dynamic-config YAML, re-read by the server every 60 s')
    asyncio.run(serve(parser.parse_args()))


if __name__ == '__main__':
    main()
