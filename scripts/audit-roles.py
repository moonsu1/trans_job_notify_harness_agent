"""Freeze reviewed real-JD role labels, then compare cached live results after a full run."""
from __future__ import annotations

import json

from career_scout.core import ROOT, Job, assessment_view, data_dir, load_config
from career_scout.policy import role_exclusion
from career_scout.storage import Store

CASES = [
    ('6d8d4d615a468691e3042f66', False, '사용자 피드백: 오퍼레이션·검수 자동화가 주업무, AI 제품 개발이 아님'),
    ('4d5b3f4b0f38506412e741e1', False, '사용자 피드백: 카탈로그 품질 검수·운영이 주업무'),
    ('0e86e8b0c9894d25aa90da35', False, '사용자 피드백: 상품 콘텐츠 제작·카피·포트폴리오가 주업무'),
    ('da73bc70588400f99611e1f9', True, '사용자 피드백: LLM Router, Agent Runtime, AI Platform 직접 개발'),
    ('8012a39938ca12fcdd6d01a1', True, '사용자 피드백: AI 에이전트 프로덕트·서비스 기획'),
    ('39c97b8a52bb4d7aef00e772', False, '결제 지표·데이터마트·Tableau·실험 분석이 주업무'),
    ('6ed711d9f7064c7d23c752e1', True, 'LLM 기반 AI 제품 포트폴리오 기획·개발 총괄; 경력 자격 충족 여부는 별도'),
    ('1c19f2570d08c0f74d15552b', False, '금융 결제 분산 백엔드가 주업무, AI는 기술 도입 항목'),
    ('ebd774513014c08843f8e7be', True, 'Agent 기반 조사·AI 워크플로·HITL·production AI 서비스 직접 개발'),
    ('5082ab6a70edb5a575b3dfb9', False, 'Zero Trust·Kubernetes·Service Mesh 보안 아키텍처가 주업무'),
    ('88c0401c44c3c7b63023ee96', False, '플랫폼 보안 아키텍처·인증·트래픽 보호가 주업무'),
    ('84f019286c90d524bc8693b2', False, 'Physical AI 로보틱스·시뮬레이션 연구 협력, 지원자 주력 Applied LLM 역할과 다름'),
    ('f7f2d9b78a1cdbdff686b72b', False, 'GPU AI Factory 클러스터 참조 아키텍처 고객 기술지원'),
    ('f5e636f5a4d75518ebdbfc54', False, 'GPU 클러스터 설계·구축 지원, Agent·AI 제품 개발 소유 역할 아님'),
    ('ea6645178e134cbd8f8f4665', False, '기반모델 사전학습·후학습·새 연구방법 및 논문 중심 연구'),
    ('5b4fbe5a8a2285752ccd2409', False, '커뮤니티 Kotlin/Spring 서버가 주업무, AI 기능 연동은 일부'),
    ('c7f2ccc63c92294cbe403c5e', False, 'React 프론트엔드 전문 개발이 주업무, Agent 도구 활용만으로 AI Engineer가 되지 않음'),
    ('4e81664bf7f957eecdbe84a8', False, '모바일·웹 QA 테스트 자동화 시스템과 테스트 시나리오가 주업무'),
    ('4a37e023ac1e5436de804f52', False, '부동산 서비스 운영정책·어뷰징 대응이 주업무, LLM은 운영 도구'),
    ('5dfb4e66f18debbf976bc0cf', True, 'LLM 라우터·에이전트 지원·ML 메타데이터/서빙 플랫폼 개발'),
    ('c1ff7c2594b9102759846421', True, 'LLM API 통합·AI 추론·출력 평가·taxonomy/ontology 직접 구현이 필수'),
    ('54289d32a77dbfb5eabacbd9', False, '일반 회원·계정·운영 어드민 플랫폼 PO; AI는 부수 개선·우대 항목'),
]


def main():
    settings, _, _ = load_config()
    store = Store(data_dir()/'scout.sqlite3')
    folder = ROOT/'evaluation'
    folder.mkdir(exist_ok=True)
    snapshot_path = folder/'role-scope-reviewed.json'
    if not snapshot_path.exists():
        rows = {r['id']: r for r in store.rows()}
        cases = []
        for key, target, reason in CASES:
            row = rows[key]
            job = Job.model_validate_json(row['payload'])
            old = json.loads(row['assessment']) if row['assessment'] else None
            cases.append({'job_id': key, 'expected_target_role': target, 'reference_reason': reason,
                          'label_origin': 'user_feedback' if len(cases) < 5 else 'JD_review',
                          'previous_score': sum(e['points'] for e in old['evidence']) if old else None,
                          'job': job.model_dump()})
        snapshot_path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding='utf-8')
    cases = json.loads(snapshot_path.read_text(encoding='utf-8'))
    rows = {r['id']: r for r in store.rows()}
    results = []
    for case in cases:
        row = rows[case['job_id']]
        job = Job.model_validate_json(row['payload'])
        exclusion = role_exclusion(job.title)
        current = row['fingerprint'] == settings['fingerprint'] and row['assessed_hash'] == row['content_hash']
        a = json.loads(row['assessment']) if current and row['assessment'] else None
        decision = (a or {}).get('role_scope', {}).get('decision')
        predicted = False if exclusion or decision == 'out_of_scope' else True if decision == 'target' else None
        results.append({'job_id': row['id'], 'title': job.title, 'expected_target_role': case['expected_target_role'],
                        'evaluated': bool(exclusion or a),
                        'predicted_target_role': predicted, 'correct': predicted == case['expected_target_role'],
                        'reference_reason': case['reference_reason'], 'previous_score': case['previous_score'],
                        'new_score': assessment_view(a, job.verified)['score'],
                        'decision_reason': exclusion or (a or {}).get('role_scope', {}).get('reason', '분석 대기')})
    complete = all(r['evaluated'] for r in results)
    accuracy = sum(r['correct'] for r in results)/len(results) if complete else None
    summary = {'case_count': len(results), 'complete': complete, 'agreement': accuracy,
               'abstained': sum(r['evaluated'] and r['predicted_target_role'] is None for r in results),
               'user_examples_correct': sum(r['correct'] for r in results[:5]), 'results': results}
    (folder/'role-scope-results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k != 'results'}, ensure_ascii=False))
    store.close()


if __name__ == '__main__':
    main()
