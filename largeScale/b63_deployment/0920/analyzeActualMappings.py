#!/usr/bin/env python3
# Evaluate exact PDF pair-normalized objective on observed/frozen actual-cluster mappings.
# Equal inter-VM cost is an explicit same-bridge logical-cost convention, not measured traffic.
"""Compare topology cut costs without passing synthetic scheduling off as Kubernetes."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path


def calculateMappingCost(topology,mapping):
    """Calculate Eq.21 with unit off-diagonal VM costs and supplied edge weights."""
    if set(mapping)!={p['id'] for p in topology['pods']}:raise ValueError('mapping lacks complete topology')
    total=0.;cutEdges=0;details=[]
    for edge in topology['networks']:
        counts=Counter(mapping[p] for p in edge['members']);size=len(edge['members'])
        if size<2:cost=0.;cross=0
        else:
            cross=math.comb(size,2)-sum(math.comb(n,2) for n in counts.values())
            cost=edge['weight']*cross/math.comb(size,2)
        cutEdges+=cross>0;total+=cost;details.append(dict(network=edge['id'],cost=cost,crossPairs=cross,nodeCounts=dict(counts)))
    return dict(cost=total,cutNetworks=cutEdges,nodeCounts=dict(Counter(mapping.values())),networks=details)


def analyzeMappings(features,mappings,output):
    """Audit explicit labelled `mappings` on one immutable `features` topology."""
    topology=json.loads(features.read_text());results={}
    for spec in mappings:
        name,path=spec.split('=',1);source=Path(path)
        if name in results:raise ValueError('duplicate experiment name')
        mapping=json.loads(source.read_text());results[name]=dict(mappingSha256=hashlib.sha256(source.read_bytes()).hexdigest(),**calculateMappingCost(topology,mapping))
    result=dict(featuresSha256=hashlib.sha256(features.read_bytes()).hexdigest(),offDiagonalCost=1,results=results,
        note='Logical weighted pair-crossing cost with explicit equal VM costs on the same bridge. Not bandwidth, traffic, latency, or a Q guarantee. Diagnostic placements are not optimizer results.')
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps({k:{n:v for n,v in r.items() if n!='networks'} for k,r in results.items()},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--features',type=Path,required=True);parser.add_argument('--mapping',action='append',required=True);parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args();analyzeMappings(a.features,a.mapping,a.output)
