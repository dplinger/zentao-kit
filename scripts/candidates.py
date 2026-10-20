"""读取当前用户的「我的任务」，作为日志候选任务来源。

数据源是「我的地盘 → 我的任务」页面（`module/my/control.php` 的 `work($mode='task')`）：
开源版即有，且一次请求就能取到任务编号、所属执行与执行名，不必解析 HTML。
取数、分页与归属校验走 my_work 的通用通道（含「非法参数被服务端当成全量」的防御）。
"""

from auth import base_url
from common import SkillError
from my_work import collect

SCOPE = 'assignedTo'    # 我负责的任务
OWNER_KEY = 'assignedTo'

# REST 里没有等价的「我的任务」入口（实测 v1/v2 tasks、v2/my/tasks 均 403），
# 页面不可用时不做自动重试，改为如实告知手动定位的步骤。
FALLBACK = ('可改用资源视角手动定位：`projects` 列出项目 → `executions --project <编号>` '
            '→ `tasks --execution <编号>`，再按任务编号登记日志。')


def fetch_candidates(session, account):
    """返回当前用户的候选任务：任务编号、名称、所属执行与执行名。"""
    base = base_url(session['base_url'])
    rows = collect(session, 'task', SCOPE, ('tasks',), OWNER_KEY, account, FALLBACK)
    result = []
    for row in rows:
        try:
            task_id, execution = int(row['id']), int(row['execution'])
            if task_id <= 0 or execution <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise SkillError('候选任务缺少任务编号或所属执行，已停止自动匹配。') from None
        result.append({'id': task_id,
                       'name': str(row.get('name') or '').strip(),
                       'execution': execution,
                       'execution_name': str(row.get('executionName') or '').strip(),
                       'url': f'{base}/task-view-{task_id}.html'})
    return result
