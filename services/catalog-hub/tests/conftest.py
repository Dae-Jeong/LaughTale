"""pytest 설정 (Laughtale 캐싱 실험).

`tests/`를 import 경로에 넣어 `oracle` 모듈을 이름으로 import할 수 있게 합니다.
`--import-mode=importlib`에서는 rootdir이 자동으로 들어가지 않으므로 여기서 지정합니다.

이 파일이 있으면 `PYTHONPATH=src:tests` 없이 `uv run --locked pytest`만으로 돌아갑니다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
