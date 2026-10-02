"""Convert a feasible model allocation to conservative Kubernetes quantities.

CPU precision is one millicore; memory is rounded down to whole bytes.
This certifies totals against input capacities only, not live admission or QoS.
"""
import math
from topologyAllocation import common_share,audit


def quantities(data,mapping,retention=1.):
 if not math.isfinite(retention) or not 0<retention<=1:raise ValueError('invalid retention')
 env=common_share(data);measured=audit(data,mapping);z=retention*env['share']
 if measured['share']+1e-9<z:raise ValueError('mapping does not meet selected entitlement')
 cpu=math.floor(z*env['nominalBundle']['cpu']*1000)
 memory=math.floor(z*env['nominalBundle']['memory']*(2**30))
 if cpu<1 or memory<1:raise ValueError('positive common quota is below deployment quantity precision')
 for h in data['hosts']:
  n=measured['counts'][h['id']]
  assert n*cpu/1000<=h['cpu']+1e-9 and n*memory/(2**30)<=h['memory']+1e-9
 return dict(cpu=str(cpu)+'m',memory=str(memory),cpuCores=cpu/1000,memoryBytes=memory,
  realizedShare=min((cpu/1000)/env['nominalBundle']['cpu'],(memory/(2**30))/env['nominalBundle']['memory']),
  requestedShare=z,liveAdmissionCertified=False,
  scope='Per-vertex quantities against input capacity; does not reserve system headroom, change manifests, or guarantee workload service.')


def reserved_quantities(data,mapping,reserve_fraction=.1):
 """Apply a fixed, input-only platform reserve to a maximum-share mapping.

 Uniformly scaling both resource pools preserves the optimal count envelope.
 The reserve is an explicit policy, not an estimate of measured system demand.
 Returned quantities are suitable for requests; admission and service remain
 separate validation questions. Unlike arbitrary entitlement retention, this
 function verifies headroom on every host.
 """
 if not math.isfinite(reserve_fraction) or not 0<=reserve_fraction<1:
  raise ValueError('reserve fraction must be in [0,1)')
 envelope=common_share(data);measured=audit(data,mapping)
 if measured['share']+1e-9<envelope['share']:
  raise ValueError('fixed reserve policy requires a maximum-share mapping')
 fraction=1-reserve_fraction;result=quantities(data,mapping,fraction);budgets=[]
 for host in data['hosts']:
  count=measured['counts'][host['id']]
  cpu=count*result['cpuCores'];memory=count*result['memoryBytes']
  cpu_budget=fraction*host['cpu'];memory_budget=fraction*host['memory']*2**30
  if cpu>cpu_budget+1e-9 or memory>memory_budget+1e-6:
   raise ValueError('rounded requests exceed the reserved host budget')
  budgets.append(dict(host=host['id'],count=count,cpuRequests=cpu,
   memoryRequests=memory,cpuBudget=cpu_budget,memoryBudget=memory_budget))
 result.update(reserveFraction=reserve_fraction,policyPoolFraction=fraction,
  poolRelativeRequestedShare=result['requestedShare']/fraction,
  poolRelativeRealizedShare=result['realizedShare']/fraction,hostBudgets=budgets,
  scope='Fixed input-capacity reserve and rounded per-vertex scheduling requests; '
        'not measured overhead, a live admission certificate, or a service guarantee.')
 return result
