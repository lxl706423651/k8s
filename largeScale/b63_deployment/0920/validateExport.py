#!/usr/bin/env python3
# Offline regression checks for deployment preservation and unsafe-export rejection.
# Uses explicitly synthetic images/inputs and never deploys to Kubernetes.
"""Verify exported node placement, graph alignment and reservation safeguards."""
import copy
import json
from pathlib import Path
import unittest

import yaml
from exportDeployment import parseQuantity,renderDeployment
from solvePlacement import PlacementModel
from validatePlacement import createFixture


def createDocuments(instance):
    """Create source fixture manifests for synthetic `instance` with preserved fields."""
    docs=[dict(apiVersion='v1',kind='ConfigMap',metadata={'name':'keep-me'},data={'payload':'unchanged'})]
    for pod in instance['pods']:
        networks=[dict(name=e['id'],ips=['192.0.2.1/24']) for e in instance['networks'] if pod['id'] in e['members']]
        docs.append(dict(apiVersion='apps/v1',kind='Deployment',metadata={'name':pod['id']},
            spec={'replicas':1,'selector':{'matchLabels':{'app':pod['id']}},'template':{
            'metadata':{'labels':{'app':pod['id']},'annotations':{'k8s.v1.cni.cncf.io/networks':json.dumps(networks)}},
            'spec':{'nodeSelector':{'kubernetes.io/hostname':'old-host'},'containers':[{
                'name':'main','image':'example.invalid/fixture:synthetic','command':['/start.sh'],
                'securityContext':{'privileged':True},'env':[{'name':'KEEP','value':'yes'}]}]}}}))
    return docs


class ExportValidation(unittest.TestCase):
    """Check safety contracts against independently mutated synthetic source data."""

    def setUp(self):
        """Build a fresh feasible model and source for each independent check."""
        self.instance=createFixture()
        self.solution=PlacementModel(self.instance).solvePlacement()
        self.docs=createDocuments(self.instance)

    def testPreservation(self):
        """Preserve non-placement fields and input objects while pinning each Pod."""
        before=copy.deepcopy(self.docs)
        output,audit=renderDeployment(self.instance,self.solution,self.docs)
        self.assertEqual(self.docs,before)
        self.assertEqual(output[0],before[0])
        for original,rendered in zip(before[1:],output[1:]):
            spec=rendered['spec']['template']['spec']
            self.assertEqual(spec['nodeName'],self.solution['mapping'][rendered['metadata']['name']])
            self.assertNotIn('nodeSelector',spec)
            for key in ('command','env','securityContext','image'):
                self.assertEqual(spec['containers'][0][key],original['spec']['template']['spec']['containers'][0][key])
            self.assertEqual(rendered['spec']['template']['metadata'],original['spec']['template']['metadata'])
        self.assertTrue(audit['audit']['feasible'])
        self.assertEqual(audit['mutableImages'],['example.invalid/fixture:synthetic'])

    def testNetworkMismatch(self):
        """Reject source network relationships absent from the optimized graph."""
        self.docs[1]['spec']['template']['metadata']['annotations']['k8s.v1.cni.cncf.io/networks']='[]'
        with self.assertRaisesRegex(ValueError,'network mismatch'):
            renderDeployment(self.instance,self.solution,self.docs)

    def testAffinity(self):
        """Reject scheduling requirements the solver has not modeled."""
        self.docs[1]['spec']['template']['spec']['affinity']={'nodeAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':{}}}
        with self.assertRaisesRegex(ValueError,'unsupported'):
            renderDeployment(self.instance,self.solution,self.docs)

    def testLimit(self):
        """Never silently increase a user's enforced container limit."""
        self.docs[1]['spec']['template']['spec']['containers'][0]['resources']={'limits':{'cpu':'1m'}}
        with self.assertRaisesRegex(ValueError,'limit'):
            renderDeployment(self.instance,self.solution,self.docs)

    def testExistingReservations(self):
        """Reject a source reservation that exceeds audited node capacity."""
        self.docs[1]['spec']['template']['spec']['containers'][0]['resources']={'requests':{'cpu':'100'}}
        with self.assertRaisesRegex(ValueError,'capacity'):
            renderDeployment(self.instance,self.solution,self.docs)

    def testAblation(self):
        """Do not export Q-disabled ablations as performance-safe artifacts."""
        self.solution['performanceConstraintEnabled']=False
        with self.assertRaisesRegex(ValueError,'Q-disabled'):
            renderDeployment(self.instance,self.solution,self.docs)

    def testObjectiveTampering(self):
        """Recompute the cost instead of trusting a stale result summary."""
        self.solution['cost']+=1
        with self.assertRaisesRegex(ValueError,'objective'):
            renderDeployment(self.instance,self.solution,self.docs)

    def testQuantityUnits(self):
        """Check distinct decimal/binary memory and sub-core CPU quantities."""
        self.assertEqual(parseQuantity('256Mi','memory'),.25)
        self.assertAlmostEqual(parseQuantity('100M','memory'),100000000/1024**3)
        self.assertEqual(parseQuantity('250m','cpu'),.25)
        with self.assertRaises(ValueError):parseQuantity('1unsupported','memory')


if __name__=='__main__':
    unittest.main(verbosity=2)
