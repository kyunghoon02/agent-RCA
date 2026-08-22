# Knowledge Retrieval Evaluation

이 benchmark는 Agent RCA 전체 정확도가 아니라 Operational Knowledge Retriever의
문서 순위 품질만 비교한다. `benchmark.yaml`의 label은 evaluation Ground Truth이므로
Agent runtime과 `knowledge/` corpus에서 접근할 수 없다.

비교 variant:

- `entity-key+lexical`: 기존 deterministic baseline
- `entity-key+vector`: semantic-only ablation
- `entity-key+lexical+vector-rrf`: target Hybrid retrieval

실행 전 승인된 PostgreSQL+pgvector instance와 `.env`의 `POSTGRES_DSN`,
`OPENAI_API_KEY`를 준비한다.

```bash
make sync-knowledge-vectors
make evaluate-knowledge-retrieval
```

결과를 보존할 때는 benchmark/corpus/model fingerprint와 함께 새 파일로 저장한다.

```bash
PYTHONPATH=src .venv/bin/python tools/evaluate_knowledge_retrieval.py \
  --output evaluation/knowledge-retrieval/results/<run-id>.json
```

현재 2개 문서와 12개 query는 실행 경로를 검증하는 pilot이다. 다음 조건을 모두 충족한
결과만 portfolio 수치 후보로 검토한다.

- human-reviewed approved 문서 20개 이상
- incident symptom, synonym/paraphrase와 negative query를 포함한 frozen query 30개 이상
- lexical/vector/Hybrid에 동일한 Context, query, Top-K와 corpus 적용
- content hash, embedding model과 benchmark fingerprint 보존
- Hit@K, Recall@K, MRR@K, nDCG@K와 p95 latency 공개
- 실패 case와 dataset 한계를 uplift 수치와 함께 공개
