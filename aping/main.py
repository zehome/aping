#!/usr/bin/env python

from pathlib import Path
import functools
import asyncio
import aiohttp
import logging
import signal
import yaml
import re


logger = logging.getLogger(__name__)
escapeseq = str.maketrans({',': '\,', ' ': '\ ', '=': '\=', '\n': ''})


class Point:
    def __init__(self, mesurement, ts=None, *, fields={}, tags={}):
        self.mesurement = mesurement
        self.fields = fields
        self.tags = tags
        self.timestamp = ts

    def __str__(self):
        return f"{self.mesurement} fields={self.fields} tags={self.tags}"

    def _encode(self, string):
        return str(string).translate(escapeseq)

    def _encode_field(self, field, value):
        f = self._encode(field)
        v = self._encode(value)
        if isinstance(value, int):
            return f"{f}={v}i"
        elif isinstance(value, (bool, float)):
            return f"{f}={v}"
        else:
            return f'{f}="{v}"'

    def encode_line(self):
        timestamp = ''  # Server calculated timestamp
        if self.timestamp:
            timestamp = ' {}'.format(self.timestamp)
        if self.tags:
            tags = ','.join(
                [''] + [
                    "{}={}".format(self._encode(k), self._encode(v))
                    for k, v in self.tags.items()
                ]
            )
        else:
            tags = ''
        mesurement = self._encode(self.mesurement)
        fields = ','.join([self._encode_field(k, v) for k, v in self.fields.items()])
        return f"{mesurement}{tags} {fields}{timestamp}\n".encode("utf-8")


class InfluxDBClient:
    def __init__(self, url, database, username, password, *, loop=None):
        auth = None
        if username and password:
            auth = aiohttp.BasicAuth(username, password)
        self.session = aiohttp.ClientSession(
            loop=asyncio.get_event_loop() if loop is None else loop,
            auth=auth)
        self.db = database
        if not url.endswith("/"):
            url += "/"
        self._url = url

    def url(self, endpoint):
        url = self._url
        if endpoint == "ping":
            return f"{url}{endpoint}"
        else:
            db = self.db
            return f"{url}{endpoint}?db={db}"

    async def ping(self):
        async with self.session.get(self.url("ping")) as r:
            return r.status == 204

    async def write(self, point):
        data = point.encode_line()
        logger.debug("write {}".format(data))
        async with self.session.post(self.url("write"), data=data) as r:
            if r.status != 204:
                logger.error("influxdb write failed: %s", await r.text())
            return r


class Dest:
    def __init__(self, name, dest, tags={}):
        self.name = name
        self.dest = dest
        self._tags = {
            "name": self.name,
            "dest": self.dest,
        }
        self._tags.update(tags)

    def tags(self, probername):
        t = self._tags.copy()
        t.update({"probername": probername})
        return t


class Prober:
    fping_re = re.compile(
        r"(?P<host>[^ ]+)\s*:.+=\s*(?P<sent>\d+)/(?P<recv>\d+)/(?P<loss>\d+)(.+=\s*(?P<min>[0-9.]+)/(?P<avg>[0-9.]+)/(?P<max>[0-9.]+))?")
    def __init__(self, probername, influxclient, dests):
        self.probername = probername
        self.dests = {d.dest: d for d in dests}  # Fast lookup
        self.process = None
        self.influxclient = influxclient
        self.stop_event = asyncio.Event()

    def __repr__(self):
        return "<Prober of {}>".format(','.join(self.dests.keys()))

    def get_process(self):
        base_cmd = [
            "fping",
            "--unreach",
            "--backoff", "1",
            "--elapsed",
            "--retry", "0",
            "--tos", "0",
            "--squiet", "10",
            "--period", "1000",
            "--random",
            "--timeout", "1000",
            "--loop"
        ]
        cmd = base_cmd + [d.dest for d in self.dests.values()]
        logger.debug("execute {}".format(' '.join(cmd)))
        return asyncio.create_subprocess_exec(
            *cmd,
            stdin=None,
            stdout=None,
            stderr=asyncio.subprocess.PIPE)

    def stop(self):
        self.stop_event.set()

    def readline(self, task):
        result = task.result()
        m = self.fping_re.match(result.decode("utf-8").strip())
        if m:
            destname = m.group("host")
            dest = self.dests.get(destname)
            if not dest:
                print("{} not found in {}".format(destname, self.dests.keys()))
                return
            asyncio.async(self.influxclient.write(
                Point(
                    "fping",
                    fields={
                        "sent": int(m.group("sent")),
                        "recv": int(m.group("recv")),
                        "loss": int(m.group("loss")),
                        "min": float(m.group("min")) if m.group("min") else 0.0,
                        "avg": float(m.group("avg")) if m.group("avg") else 0.0,
                        "max": float(m.group("max")) if m.group("max") else 0.0,
                    },
                    tags=dest.tags(self.probername),
                )
            ))

    def stopped(self, process, task):
        if process.returncode is None:
            process.terminate()

    async def run(self):
        process = await self.get_process()
        stop_future = asyncio.ensure_future(self.stop_event.wait())
        stop_future.add_done_callback(functools.partial(self.stopped, process))
        while not self.stop_event.is_set():
            readline_future = asyncio.ensure_future(process.stderr.readline())
            readline_future.add_done_callback(self.readline)
            await asyncio.wait([
                readline_future,
                stop_future,
                process.wait()],
                return_when=asyncio.FIRST_COMPLETED)
            if process.returncode is not None and not self.stop_event.is_set():
                process = await self.get_process()
        await process.wait()
        return self


def parse_conf(yamlf):
    confdata = yaml.load(yamlf)
    return confdata


def chunker(l, pool):
    lists = [[] for x in range(pool)]
    for i, e in enumerate(l):
        lists[i % pool].append(e)
    return lists


def get_fping_probers(conf, influxclient, worker_count):
    dests = []
    for name, params in conf["probes"].items():
        if isinstance(params.get("fping", []), str):
            fpings = [params["fping"]]
        else:
            fpings = params["fping"]
        dests.extend([
            Dest(name=name, dest=dest, tags=params.get("tags", {}))
            for dest in fpings
        ])
    return [
        Prober(
            probername=conf["prober"]["name"],
            influxclient=influxclient,
            dests=pool_dests
        )
        for pool_dests in chunker(dests, worker_count)
    ]


async def monitor_tasks(loop, worker_count, conffile):
    conf = parse_conf(conffile)
    influxconf = conf["output"]["influxdb"]
    influxclient = InfluxDBClient(
        url=influxconf["url"],
        database=influxconf["database"],
        username=influxconf["username"],
        password=influxconf["password"])
    print("Testing InfluxDB connection: {}".format(
        "OK" if await influxclient.ping() else "FAILED"))

    reload_event = asyncio.Event(loop=loop)
    loop.add_signal_handler(signal.SIGHUP, reload_event.set)
    probers = get_fping_probers(
        conf, influxclient=influxclient, worker_count=worker_count)
    tasks = [
        loop.create_task(prober.run())
        for prober in probers
    ]
    while True:
        await reload_event.wait()
        if reload_event.is_set():
            print("reload")
            conffile.seek(0)
            conf = parse_conf(conffile)
            reload_event.clear()
            for p in probers:
                p.stop()
            for task in tasks:
                task.cancel()
            probers = get_fping_probers(
                conf, influxclient=influxclient, worker_count=worker_count)
            tasks = [
                loop.create_task(prober.run())
                for prober in probers
            ]


def main():
    import argparse
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "-w", "--workers", type=int, default=10,
        help="parallel probes")
    parser.add_argument(
        "-c", "--config", type=argparse.FileType('r', encoding='utf-8'),
        help="configuration",
        default=str(Path.home() / ".aping" / "config.yml"))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(monitor_tasks(loop, args.workers, args.config))


if __name__ == "__main__":
    main()
