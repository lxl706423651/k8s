#!/usr/bin/env python3
# Build a source-hashed review table from completed target-node model/test artifacts.
# Reads results only and writes an exclusive snapshot directory; never fits or deploys.
"""Keep prediction accuracy, empirical admission and physical validation distinct."""
import argparse
import hashlib
import json
from pathlib import Path
from fitResponseSurface import selectValid


def summarizeEvidence(root, output):
    """Verify completed evidence under `root`; label absent revision evidence as pending."""
    rows=[];sources={}
    for profile,prefix in [('c2m2','node-c2m2'),('c4m2','node-c4m2'),('c4m4','node-c4m4'),('c4m2 revised','node-c4m2-refined')]:
        modelPath=root/(prefix+'-model.json');testPath=root/(prefix+'-test-evaluation.json')
        row=dict(profile=profile,status='pending_calibration',accuracyPassed=None,mae=None,p95=None,
                 admitted=None,violations=None,completeProfileAcceptance=False,actualModelAdmittedDeploymentValidated=False)
        if modelPath.exists():
            model=json.loads(modelPath.read_text());digest=hashlib.sha256(modelPath.read_bytes()).hexdigest()
            sources[str(modelPath)]=digest
            row.update(baselineLowerQ=model['baselineLowerQ'],modelSha256=digest,status='pending_test')
            if model.get('eligibility')=='ineligible_no_safe_region':
                if testPath.exists():raise ValueError('unexpected test after guard stop')
                row['status']='calibration_guard_stopped'
            elif testPath.exists():
                test=json.loads(testPath.read_text());m=test['metrics']
                raw=root/(prefix+'-test')/profile.split()[0]/'raw.jsonl'
                _,audit=selectValid(raw)
                if test['modelSha256']!=digest or audit['sha256']!=test['audit']['sha256']:
                    raise ValueError('evidence hash mismatch')
                if audit['sha256'] in [a['sha256'] for a in model['trainingAudits']]+[model['calibrationAudit']['sha256']]:
                    raise ValueError('test is not independent of model fitting')
                sources[str(testPath)]=hashlib.sha256(testPath.read_bytes()).hexdigest();sources[str(raw)]=audit['sha256']
                row.update(status='test_completed',accuracyPassed=test['accuracyPassed'],mae=m['mae'],p95=m['p95AbsoluteError'],
                    admitted=m['predictedSafePoints'],violations=m['unsafeAmongPredictedSafe'],audit=audit,
                    completeProfileAcceptance=bool(test['accuracyPassed'] and m['predictedSafePoints'] and not m['unsafeAmongPredictedSafe']))
        rows.append(row)
    output.mkdir(parents=True,exist_ok=False)
    report=dict(rows=rows,sources=sources,sourceSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        cpuTrackingWindow='Injector lifetime, including pressure-only warmup; not a separately metered canary-only window or instantaneous CPU cap.',
        note='Test points and temporal blocks are not independent deployments. None of these artifacts by itself certifies physical workload transfer.')
    (output/'evidence.json').write_text(json.dumps(report,indent=2)+'\n')
    lines=['# Target-worker evidence snapshot','', '| Profile | Status | MAE | P95 error | Accuracy passed | Admitted / violations | Complete profile acceptance |',
           '|---|---|---:|---:|---|---|---|']
    for r in rows:
        number=lambda key:'—' if r[key] is None else f'{r[key]:.6f}'
        count='—' if r['admitted'] is None else f"{r['admitted']} / {r['violations']}"
        lines.append(f"| {r['profile']} | {r['status']} | {number('mae')} | {number('p95')} | {r['accuracyPassed']} | {count} | {r['completeProfileAcceptance']} |")
    lines+=['',report['cpuTrackingWindow'],'',report['note'],'','Exact source hashes and full raw-data gates: `evidence.json`.']
    (output/'evidence.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('results'));p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();summarizeEvidence(a.root,a.output)
