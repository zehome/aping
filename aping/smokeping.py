#!/usr/bin/env python

from pathlib import Path
import argparse
import yaml
import re

INCLUDE_RE = re.compile(r"^\s*@include\s+([^ ]+)")
HIERARCHY_RE = re.compile(r"^\s*([+]+)\s*([^ ]+)")
HOST_RE = re.compile(r"host\s*=\s*([^/ ]+)")


class Host():
    def __init__(self, hierarchy, dest):
        self.hierarchy = hierarchy
        self.dest = dest

    @property
    def name(self):
        return '_'.join(self.hierarchy)

    def to_dict(self):
        return {
            'fping': [self.dest],
            'tags': {
                'client': self.hierarchy[-2]
            } if len(self.hierarchy) > 2 else {}
        }


def parse_targets(f, hierarchy=[]):
    hosts = []
    for line in f.readlines():
        current_level = len(hierarchy)
        line = line.strip()
        include_match = INCLUDE_RE.match(line)
        if include_match:
            inc_path = Path(include_match.group(1))
            try:
                with inc_path.open(encoding="utf-8") as incf:
                    hosts.extend(parse_targets(incf, hierarchy[:]))
                    continue
            except IOError:
                print("Unable to open {}".format(inc_path.resolve()))
        hierarchy_match = HIERARCHY_RE.match(line)
        if hierarchy_match:
            level = len(hierarchy_match.group(1).split("+")) - 1
            name = hierarchy_match.group(2)
            if level <= current_level:
                # Change hierarchy
                for i in range((current_level - level) + 1):
                    hierarchy.pop()
            hierarchy.append(name)
            continue
        if hierarchy:
            host_match = HOST_RE.match(line)
            if host_match:
                hosts.append(Host(hierarchy[:], host_match.group(1)))
    return hosts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "smokeping_targets",
        type=argparse.FileType('r', encoding='utf-8'))
    args = parser.parse_args()
    hosts = parse_targets(args.smokeping_targets)
    finaldict = {"probes": {h.name: h.to_dict() for h in hosts}}
    print(yaml.dump(finaldict, default_flow_style=False))

if __name__ == "__main__":
    main()
