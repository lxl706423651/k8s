#!/usr/bin/env python3
# Offline pre-deployment audit using a completed empty-canary window and fresh cluster snapshots.
# Re-expresses existing frozen surfaces; never refits Q or updates a mapping automatically.
"""Reject stale-background admission before executing a real placement trial."""
import copy
import json

from analyzeRoutingCanary import readWindow
from buildDeploymentDiagnostic import estimateBackground
from exportDeployment import parseQuantity
from solvePlacement import PlacementModel


def calculatePodRequest(pod, key):
    """Account for regular/non-restartable-init requests plus Pod overhead for `key`."""
    spec=pod['spec'];init=spec.get('initContainers',[])
    if any(c.get('restartPolicy')=='Always' for c in init):raise ValueError('restartable init request accounting unsupported')
    request=lambda c:parseQuantity(c.get('resources',{}).get('requests',{}).get(key,'0'),key)
    regular=sum(request(c) for c in spec['containers'])
    initialization=max((request(c) for c in init),default=0.)
    return max(regular,initialization)+parseQuantity(spec.get('overhead',{}).get(key,'0'),key)


def refreshBackground(instance, windows, liveNodes, livePods):
    """Refresh `instance` from per-profile window paths and current Kubernetes snapshots.

    Capacities can only decrease; frozen response-axis coordinates are rescaled to
    preserve their physical meaning. Background changes shift the query, not the fit.
    """
    refreshed=copy.deepcopy(instance);evidence={}
    if len({n['profile'] for n in refreshed['nodes']})!=len(refreshed['nodes']):
        raise ValueError('background refresh requires a distinct calibrated surface per worker')
    nodes={n['metadata']['name']:n for n in liveNodes['items']}
    for node in refreshed['nodes']:
        name=node['id'];profile=name.removeprefix('placement-deploy0920-')
        if profile not in windows or name not in nodes:raise ValueError('missing live node/reference')
        live=nodes[name]
        if not any(c['type']=='Ready' and c['status']=='True' for c in live['status']['conditions']):raise ValueError('node not Ready')
        if live['spec'].get('unschedulable') or any(t['effect'] in ['NoSchedule','NoExecute'] for t in live['spec'].get('taints',[])):
            raise ValueError('node scheduling eligibility changed')
        window=windows[profile];_,identity,rawHash=readWindow(window,profile)
        expected=node['measurementProtocol']
        if (identity['canaryAstSha256']!=node['canaryAstSha256'] or identity['seconds']!=expected['measurementSeconds'] or
                identity['warmup']!=expected['warmupSeconds'] or identity['warmupMode']!=expected['warmupMode'] or
                identity['pressureControl']!='cumulative_cpu_debt_v1' or identity['memoryReserveFraction']!=.15):
            raise ValueError('pre-deployment reference uses another measurement protocol')
        manifest=json.loads((window/'manifest.json').read_text())
        if len(manifest['profiles'])!=1 or manifest['profiles'][0]['profile']['name']!=name:raise ValueError('reference VM identity changed')
        background=estimateBackground(window/profile/'raw.jsonl')
        system=[p for p in livePods['items'] if p['spec'].get('nodeName')==name and p['status']['phase'] not in ['Succeeded','Failed']]
        available={key:parseQuantity(live['status']['allocatable'][key],key)-sum(calculatePodRequest(p,key)
            for p in system) for key in ['cpu','memory']}
        oldCpu,oldMemory=node['cpu'],node['memory']
        # Legacy frozen instances omit this field and retain their original 64MiB policy.
        reserveFraction=node.get('memoryReserveFraction',0.)
        if 'memoryReserveFraction' in node and reserveFraction!=identity['memoryReserveFraction']:
            raise ValueError('instance/instrument memory reserve mismatch')
        memoryReserve=max(reserveFraction*background['guestMemoryGiB'],.0625)
        node['cpu']=min(oldCpu,background['guestCpu']-1-max(.1,background['systemCpuP95']),available['cpu'])
        node['memory']=min(oldMemory,background['guestMemoryGiB']-background['memoryProxyP95GiB']-memoryReserve,available['memory'])
        if min(node['cpu'],node['memory'])<=0:raise ValueError('no remaining Pod capacity')
        node['baseCpu']=1+background['systemCpuMedian'];node['baseMemory']=background['memoryProxyMedianGiB']
        node['slots']=min(node['slots'],int(live['status']['allocatable']['pods'])-len(system))
        surface=refreshed['surfaces'][node['profile']]
        surface['cpu']=[x*oldCpu/node['cpu'] for x in surface['cpu']]
        surface['memory']=[x*oldMemory/node['memory'] for x in surface['memory']]
        evidence[name]=dict(background=background,referenceSha256=rawHash,oldCapacity=dict(cpu=oldCpu,memory=oldMemory),
            refreshedCapacity=dict(cpu=node['cpu'],memory=node['memory']),systemPodCount=len(system),kubernetesUnrequested=available)
    PlacementModel(refreshed)
    return refreshed,evidence
