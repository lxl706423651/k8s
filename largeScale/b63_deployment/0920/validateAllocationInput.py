"""Validate the capacity-only schema in linear membership work.

Same accepted schema as topologyAllocation.validate. Build the vertex set once,
not once per LAN. No caching across calls, mutation, workload inputs or VM effects.
"""
import math


def validateAllocationInput(data):
    """Validate data containing only hosts, vertices and unweighted LAN members."""
    if set(data) != {'hosts', 'vertices', 'networks'}:
        raise ValueError('only hosts, vertices, networks are accepted')
    ids = data['vertices']
    hosts = data['hosts']
    known = set(ids)
    if not ids or len(known) != len(ids) or not hosts or len({h['id'] for h in hosts}) != len(hosts):
        raise ValueError('nonempty unique vertex and host IDs required')
    for host in hosts:
        if set(host) != {'id', 'cpu', 'memory'} or any(not math.isfinite(host[r]) or host[r] <= 0 for r in ['cpu', 'memory']):
            raise ValueError('host inputs are positive CPU/memory capacities only')
    for edge in data['networks']:
        if set(edge) != {'id', 'members'} or not edge['members']:
            raise ValueError('invalid unweighted network membership')
        members = set(edge['members'])
        if len(members) != len(edge['members']) or not members <= known:
            raise ValueError('invalid unweighted network membership')
