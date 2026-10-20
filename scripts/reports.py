"""禅道统计报表：读取内建统计（`GET v1/reports`）。

这是**隐藏接口**（未进官方文档），且对未知字段名**静默忽略**——字段名拼错不会报错，
只会返回空对象，用户会误以为「就是没有数据」。因此字段名一律在本地白名单校验
（属同一类静默出错风险）。

另一个容易误读的点：接口默认统计**全部用户**，`account` 只影响 projectoverview / radar /
output 三项；progress 类是全局的，指定账号也不会收窄。要看个人数据必须显式给 `--me`
或 `--account`，输出里也会写明本次统计范围。
"""

import datetime as dt
import re

from common import SkillError

ENDPOINT = 'v1/reports'
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
# 字段 → (中文名, 说明, 响应里的键名, 明细数组的键名或 None)
FIELDS = {
    'projectoverview': ('项目概况', '各状态项目数量汇总', 'projectOverview', None),
    'radar': ('年度贡献雷达', '产品/执行/研发/测试/其他五维贡献', 'radar', None),
    'projectprogress': ('项目进度', '项目列表与进度、工时', 'projectProgress', 'projects'),
    'executionprogress': ('执行进度', '执行列表与进度、工时、延期', 'executionProgress', 'executions'),
    'productprogress': ('产品进度', '产品列表与需求状态分布', 'productProgress', 'products'),
    'bugprogress': ('Bug 进度', '产品列表与 Bug 状态分布', 'bugProgress', 'bugs'),
    'output': ('年度产出', '按操作类型统计的年度产出', 'output', None),
}
# 明细会随记录量线性膨胀（实测可达数百条、50KB 级），这些字段必须按 limit 截断后再输出。
SCANNED = ('projectprogress', 'executionprogress', 'productprogress', 'bugprogress')


def catalog():
    """列出可用统计项，供宿主自省，不必猜字段名。"""
    return {'status': 'ok',
            'message': '用 report --fields <逗号分隔的统计项> 取数；不带 --fields 时只列出本表。',
            'fields': [{'field': name, 'label': item[0], 'detail': item[1]}
                       for name, item in FIELDS.items()]}


def parse_fields(value):
    """解析并校验统计项；接口会静默忽略未知字段，这里必须先拦住。"""
    names, seen = [], set()
    for part in str(value or '').split(','):
        name = part.strip().lower()
        if not name or name in seen:
            continue
        if name not in FIELDS:
            raise SkillError(f'不支持的统计项「{name}」；可用项：' + '、'.join(FIELDS) + '。')
        seen.add(name)
        names.append(name)
    if not names:
        raise SkillError('请用 --fields 指定统计项，或先运行 report 查看可用项。')
    return names


def parse_year(value):
    year = int(value) if value is not None else dt.date.today().year
    if not re.fullmatch(r'\d{4}', str(year)) or not 2000 <= year <= 2100:
        raise SkillError('年份应为 2000 至 2100 之间的四位数。')
    return year


def parse_limit(value):
    limit = int(value) if value is not None else DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise SkillError(f'--limit 应在 1 至 {MAX_LIMIT} 之间；明细列表按此条数截断。')
    return limit


def verify_account(client, account):
    """确认账号存在。

    接口对不存在的账号不报错，只会把统计全部算成 0——用户拿到的是「看起来正常」的错数据。
    宁可多一次请求，也不能给错数据（同 query 的范围预检）。
    """
    response = client.call('GET', 'v2/users?pageID=1&recPerPage=500')
    users = response.get('users') if isinstance(response, dict) else response
    if not isinstance(users, list):
        raise SkillError('用户列表响应格式不匹配，无法核对账号；已停止。')
    if account not in {row.get('account') for row in users if isinstance(row, dict)}:
        total = (response.get('pager') or {}).get('recTotal') if isinstance(response, dict) else None
        if total and int(total) > len(users):
            # 人数超出一页时无法在本地断言「不存在」，如实说明而不是硬判。
            raise SkillError(f'用户过多（{total} 人）一页取不全，无法核对账号 {account}；'
                             '请确认账号拼写，或用 query --entity users 查找。')
        raise SkillError(f'没有找到账号 {account}；统计未执行。用 query --entity users 可查找账号。')


def fetch(client, fields, year, account, limit):
    """读取统计并压缩输出：明细列表按 limit 截断，计数型全给。"""
    names = parse_fields(fields)
    year = parse_year(year)
    limit = parse_limit(limit)
    account = (account or '').strip()
    if account:
        verify_account(client, account)
    params = {'fields': ','.join(names), 'year': str(year)}
    if account:
        params['account'] = account
    path = ENDPOINT + '?' + '&'.join(f'{key}={value}' for key, value in params.items())
    response = client.call('GET', path)
    if not isinstance(response, dict):
        raise SkillError('报表接口未返回有效结构；不能据此认为没有数据。')

    sections, notes = {}, []
    for name in names:
        label, _, key, rows_key = FIELDS[name]
        section = response.get(key)
        if section is None:
            raise SkillError(f'报表响应里没有 {key}（对应统计项 {name}），'
                             '接口结构可能已变化；不能当成「没有数据」。')
        if rows_key and isinstance(section, dict) and isinstance(section.get(rows_key), list):
            rows = section[rows_key]
            section = dict(section)
            section['total'] = len(rows)
            section[rows_key] = rows[:limit]
            if len(rows) > limit:
                notes.append(f'{label}共 {len(rows)} 条，本次只列出前 {limit} 条（用 --limit 调整）')
        sections[key] = section

    scope = f'账号 {account}' if account else '全部用户'
    notes.append(f'统计范围为{scope}，年份 {year}'
                 + ('（指定账号只影响项目概况、雷达、年度产出三项，进度类统计始终是全局的）' if account and set(names) & set(SCANNED) else ''))
    notes.append('报表来自内建接口，属隐藏能力，不同版本可能不一致')
    return {'status': 'ok', 'year': year, 'scope': scope, 'fields': names,
            'sections': sections, 'message': '；'.join(notes) + '。'}
