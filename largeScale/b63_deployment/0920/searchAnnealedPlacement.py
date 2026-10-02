"""Quality-first count-preserving swap/cycle search on original LAN connectivity.
Topology/capacity inputs only; temporary worsening is allowed, best audited mapping returned.
"""
import math,random,time
from collections import Counter
from topologyAllocation import audit,common_share


class PartitionState:
    """Maintain original LAN host counts and O(1) membership lists under balanced exchanges."""
    def __init__(self,data,mapping):
        self.data=data;self.vertices=data['vertices'];self.hosts=[h['id'] for h in data['hosts']];idx={v:i for i,v in enumerate(self.vertices)};self.assignment=[self.hosts.index(mapping[v]) for v in self.vertices];self.members=[[idx[v] for v in e['members']] for e in data['networks']];self.incident=[[] for _ in self.vertices];self.counts=[];self.hostNodes=[[] for _ in self.hosts];self.positions=[0]*len(self.vertices)
        for v,h in enumerate(self.assignment):self.positions[v]=len(self.hostNodes[h]);self.hostNodes[h].append(v)
        for e,members in enumerate(self.members):
            counts=[0]*len(self.hosts)
            for v in members:self.incident[v].append(e);counts[self.assignment[v]]+=1
            self.counts.append(counts)
        self.spans=[sum(c>0 for c in row)-1 for row in self.counts];self.cost=sum(self.spans);self.cutEdges=[];self.cutPositions={}
        for e,span in enumerate(self.spans):
            if span:self.cutPositions[e]=len(self.cutEdges);self.cutEdges.append(e)

    def proposeExchange(self,rng):
        """Use rng to propose a two-host swap or three-host cycle, preserving all host counts."""
        if not self.cutEdges:return {}
        edge=rng.choice(self.cutEdges);v=rng.choice(self.members[edge]);h=self.assignment[v];others=[j for j,nodes in enumerate(self.hostNodes) if j!=h and nodes]
        if not others:return {}
        second=rng.choice(others)
        if len(others)>1 and rng.random()<.2:
            third=rng.choice([j for j in others if j!=second]);u=rng.choice(self.hostNodes[second]);w=rng.choice(self.hostNodes[third]);return {v:second,u:third,w:h}
        local=[u for u in self.members[edge] if self.assignment[u]!=h]
        u=rng.choice(local) if local and rng.random()<.7 else rng.choice(self.hostNodes[second]);return {v:self.assignment[u],u:h}

    def evaluateExchange(self,moves):
        """Return exact cost delta and changed per-LAN counts for node-index -> new-host moves."""
        changed={}
        for v,newHost in moves.items():
            oldHost=self.assignment[v]
            for edge in self.incident[v]:
                row=changed.setdefault(edge,list(self.counts[edge]));row[oldHost]-=1;row[newHost]+=1
        delta=sum(sum(c>0 for c in row)-1-self.spans[e] for e,row in changed.items())
        return delta,changed

    def applyExchange(self,moves,delta,changed):
        """Apply a previously evaluated balanced exchange and update boundary/host indices."""
        for v in moves:
            h=self.assignment[v];position=self.positions[v];last=self.hostNodes[h].pop()
            if last!=v:self.hostNodes[h][position]=last;self.positions[last]=position
        for v,h in moves.items():self.positions[v]=len(self.hostNodes[h]);self.hostNodes[h].append(v);self.assignment[v]=h
        for e,row in changed.items():
            span=sum(c>0 for c in row)-1;self.counts[e]=row;self.spans[e]=span
            if span and e not in self.cutPositions:self.cutPositions[e]=len(self.cutEdges);self.cutEdges.append(e)
            elif not span and e in self.cutPositions:
                position=self.cutPositions.pop(e);last=self.cutEdges.pop()
                if last!=e:self.cutEdges[position]=last;self.cutPositions[last]=position
        self.cost+=delta

    def buildMapping(self):
        """Return the complete original vertex/host mapping represented by this state."""
        return {v:self.hosts[self.assignment[i]] for i,v in enumerate(self.vertices)}

    def verifyState(self):
        """Independently recalculate original LAN costs, host memberships and boundary indices."""
        expected=[]
        for members in self.members:
            counts=Counter(self.assignment[v] for v in members);expected.append([counts[h] for h in range(len(self.hosts))])
        assert expected==self.counts and self.cost==sum(sum(c>0 for c in row)-1 for row in expected)
        assert set(self.cutEdges)=={i for i,row in enumerate(expected) if sum(c>0 for c in row)>1}
        for i,e in enumerate(self.cutEdges):assert self.cutPositions[e]==i
        for h,nodes in enumerate(self.hostNodes):
            for i,v in enumerate(nodes):assert self.assignment[v]==h and self.positions[v]==i
        assert sorted(v for nodes in self.hostNodes for v in nodes)==list(range(len(self.vertices)))


def searchAnnealedPlacement(data,mapping,seed=1,steps=300000,cycleSteps=50000,temperatures=(.35,.7,1.4),progress=None):
    """Run fixed-count exchanges; cool each cycle and restart from the best feasible mapping."""
    if steps<=0 or cycleSteps<=0 or not temperatures or min(temperatures)<0:raise ValueError('invalid schedule')
    initial=audit(data,mapping);caps=common_share(data)['countCaps'];hosts=[h['id'] for h in data['hosts']]
    if any(initial['counts'][h]>cap for h,cap in zip(hosts,caps)):raise ValueError('infeasible incumbent')
    state=PartitionState(data,mapping);best=dict(mapping);bestCost=state.cost;rng=random.Random(seed);start=time.monotonic();trace=[];improvements=[];accepted=0;worsening=0;peak=state.cost
    for step in range(steps):
        position=step%cycleSteps;cycle=step//cycleSteps
        if position==0:state=PartitionState(data,best)
        base=temperatures[cycle%len(temperatures)];temperature=base*(.02/base)**(position/max(1,cycleSteps-1)) if base>0 else 0
        moves=state.proposeExchange(rng)
        if not moves:break
        delta,changed=state.evaluateExchange(moves)
        if delta<=0 or (temperature>0 and rng.random()<math.exp(-delta/temperature)):
            state.applyExchange(moves,delta,changed);accepted+=1;worsening+=int(delta>0);peak=max(peak,state.cost)
            if state.cost<bestCost:state.verifyState();best=state.buildMapping();bestCost=state.cost;improvements.append(dict(step=step+1,cost=bestCost,seconds=time.monotonic()-start))
        if (step+1)%10000==0:state.verifyState()
        if (step+1)%cycleSteps==0 or step+1==steps:
            state.verifyState();row=dict(step=step+1,current=state.cost,best=bestCost,temperature=base,accepted=accepted,worsening=worsening,peak=peak,seconds=time.monotonic()-start);trace.append(row)
            if progress:progress(row)
    measured=audit(data,best);assert measured['connectivity']==bestCost and measured['counts']==initial['counts']
    return dict(mapping=best,cost=bestCost,initialCost=initial['connectivity'],counts=measured['counts'],seed=seed,steps=steps,cycleSteps=cycleSteps,temperatures=list(temperatures),trace=trace,improvements=improvements,accepted=accepted,worsening=worsening,peak=peak,seconds=time.monotonic()-start)
