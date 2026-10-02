"""Capacity-only allocation with KaHyPar and optional monotone local improvement."""
import argparse,json,time
from pathlib import Path
from topologyAllocationKaHyPar import solve as partition
from topologyRefine import refine as moves
from topologySwapRefine import refine as swaps


def solve(data,configuration,seed=11,retention=1.,refine_seconds=2.):
 start=time.monotonic();base=partition(data,configuration,retention,seed)
 if not base['valid']:raise RuntimeError('KaHyPar returned a capacity-infeasible partition')
 moved=moves(data,base['mapping'],retention,seconds=refine_seconds)
 final=swaps(data,moved['mapping'],seconds=refine_seconds)
 assert final['share']+1e-8>=base['capacityEnvelope']['share']*retention
 return dict(base=base,moves=moved,final=final,totalSeconds=time.monotonic()-start)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('instance',type=Path)
 p.add_argument('--configuration',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--seed',type=int,default=11);p.add_argument('--retention',type=float,default=1.)
 args=p.parse_args();result=solve(json.loads(args.instance.read_text()),args.configuration,args.seed,args.retention)
 with args.output.open('x') as f:json.dump(result,f,indent=2)
 print(json.dumps(dict(initial=result['base']['connectivity'],final=result['final']['connectivity'],seconds=result['totalSeconds'])))
