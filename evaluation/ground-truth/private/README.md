# Private Ground Truth

Fault injection 시각, 실제 root cause label과 평가용 정답은 이 디렉터리의 로컬 파일에 저장한다.

- Incident Platform Pod에 이 디렉터리를 mount하지 않는다.
- RCA ServiceAccount와 LLM tool은 이 경로를 읽을 수 없어야 한다.
- 평가기는 RCA 실행이 종료된 뒤에만 이 데이터를 읽는다.
- 실제 label 파일은 기본적으로 Git에 commit하지 않는다.
- 공개 가능한 synthetic label은 redaction 후 별도 evaluation artifact로 내보낸다.
