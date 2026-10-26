"""Offline acceptance counterexamples: real view functions and eval-list endpoint.

Uses Node's TS stripping, a temporary JSON file and an in-process path patch.
No server, model, cloud, SSH or Milvus requests are made.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.api import runs


def view_probe():
    script = """
import { targetCandidates, targetRequestBody } from './src/view.ts';
const choices = targetCandidates([
 {service:'orders',instance_id:'i-a',region:'r-a'},
 {service:'orders',instance_id:'i-b',region:'r-b'}
]);
console.log(JSON.stringify({input_candidates:2,output_candidates:choices,
 request:targetRequestBody({service_name:'orders'},choices[0]?.service)}));
"""
    completed = subprocess.run(
        ['node', '--experimental-strip-types', '--input-type=module'],
        input=script, cwd=ROOT/'web', capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30, check=True,
    )
    result = json.loads(completed.stdout)
    result['bug_reproduced'] = len(result['output_candidates']) != result['input_candidates']
    return result


def eval_probe():
    with tempfile.TemporaryDirectory(prefix='oncall-eval-audit-') as temporary:
        folder = Path(temporary)
        (folder/'fixture.json').write_text(json.dumps({
            'manifest': {'execution_mode':'local'},
            'rows': [{'task_success':True, 'tool_f1':1.0}],
        }), encoding='utf-8')
        with patch.object(runs, 'RESULT_DIR', folder):
            result = asyncio.run(runs.list_eval_results(limit=20))
    return {'input_rows':1, 'api_result':result,
            'bug_reproduced':not result['results']}


if __name__ == '__main__':
    print(json.dumps({'same_service_candidates':view_probe(),
                      'new_eval_format':eval_probe()}, ensure_ascii=False, indent=2))
