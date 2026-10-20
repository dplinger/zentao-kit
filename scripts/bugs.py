"""读取当前用户 Bug JSON 列表；只输出已校验的摘要字段。

取数与校验走 my_work（「我的地盘」页面通用通道，含分页核对与归属校验）；
这里只负责 Bug 特有的字段校验与展示。
"""

from html.parser import HTMLParser

from bug_actions import redact
from common import SkillError
from my_work import collect


SCOPES = ('assignedTo', 'openedBy', 'resolvedBy', 'closedBy')

# 「我的 Bug」是人的视角，REST 里没有等价入口（实测 v1/v2 tasks、v2/my/* 均 403）。
# 因此页面不可用时不做自动重试，而是把能走通的资源视角路径如实告知。
FALLBACK = ('可改用资源视角查询：`query --entity bugs --product <产品编号>`'
            '（也可用 --project 或 --execution 指定范围），单条用 `bug-detail --bug <编号>`。')


def _verify(rows):
    """Bug 特有的轻量校验：标题与状态必须是字符串，结构变化不能当成没有数据。"""
    for row in rows:
        if not isinstance(row.get('title'), str) or not isinstance(row.get('status'), str):
            raise SkillError('Bug 记录缺少标题或状态，接口结构可能已变化，已停止；'
                             '不能视为没有 Bug。' + FALLBACK)


class Description(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.images = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'br', 'tr', 'li'):
            self.text.append('\n')
        if tag == 'img':
            self.images.append(dict(attrs).get('src', ''))

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def fetch_bugs(session, account, bug=None, scope='assignedTo'):
    """全量核对分页后输出精简摘要；指定编号时附上描述与图片引用。"""
    if scope not in SCOPES:
        raise SkillError('Bug 查询范围不支持。')
    # 归属由 my_work 逐条核对：实测非法的范围参数会让服务端返回全部人的 Bug。
    rows = collect(session, 'bug', scope, ('bugs',), scope, account, FALLBACK)
    _verify(rows)
    output = []
    for row in rows:
        if bug is not None and int(row['id']) != bug:
            continue
        item = {k: row.get(k) for k in ('id', 'title', 'product', 'project', 'execution', 'status', 'assignedTo', 'severity', 'pri')}
        item['title'] = redact(item['title'])
        item['url'] = session['base_url'].rstrip('/') + '/bug-view-' + str(row['id']) + '.html'
        if bug is not None:
            parser = Description()
            parser.feed(str(row.get('steps') or ''))
            item['description'] = redact('\n'.join(x.strip() for x in ''.join(parser.text).splitlines() if x.strip()))
            item['images'] = [redact(src) for src in parser.images]
        output.append(item)
    if bug is not None and not output:
        raise SkillError('该编号不在当前查询范围的 Bug 列表中。')
    return {'status': 'ok', 'scope': scope, 'total': len(rows), 'bugs': output}
