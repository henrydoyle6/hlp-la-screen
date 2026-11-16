"""User-defined, per-takeover U/M. This module does not change production math."""
from decimal import Decimal,Inexact,localcontext

EXTRA_HEADERS=['q_有符号接管数量','U','M','M未计算原因']

def number(value):
    result=Decimal(str(value))
    if not result.is_finite():raise ValueError('Input must be a finite decimal')
    return result

def text(value):
    if value==0:return '0'
    result=format(value,'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result

def precision_for(account,entry,mark,quantity):
    """Bound the full digit span of account - (mark - entry) * quantity."""
    exponent=lambda value:value.as_tuple().exponent
    lower=min(exponent(account),exponent(mark)+exponent(quantity),exponent(entry)+exponent(quantity))
    upper=max(account.adjusted()+1,mark.adjusted()+quantity.adjusted()+2,entry.adjusted()+quantity.adjusted()+2)
    difference=max(mark.adjusted(),entry.adjusted())+2-min(exponent(mark),exponent(entry))
    return max(34,upper-lower+3,difference)

def calculate_event(row):
    """Return signed q, U, M and a Chinese reason; never use proxy price columns."""
    if row['记录类型']!='fill':
        return ['','','','原有输入诊断记录（非接管事件），q、U和M不适用。']
    direction=row['接管方向'];quantity=number(row['接管数量'])
    if direction not in ('long','short') or quantity<=0:raise ValueError('Invalid takeover direction or quantity')
    q=quantity if direction=='long' else quantity.copy_negate()
    entry_value=row['被接管用户平均开仓价'];mark_value=row['清算标记价'];account_value=row['accountValue']
    reasons=[]
    if entry_value=='':
        reasons.append('缺少被接管用户平均开仓价；具体原因：'+(row.get('平均开仓价未补全原因') or '该笔主均价列为空，未用损益隐含参考值替代。'))
    if mark_value=='':
        reasons.append('缺少原始清算标记价；具体原因：'+(row.get('清算标记价未补全原因') or '该笔原始标记价列为空，未用账本隐含参考值替代。'))
    if account_value=='':reasons.append('缺少accountValue：该笔账户价值列为空。')
    if entry_value=='' or mark_value=='':return [text(q),'','','无法计算M：'+'；'.join(reasons)]
    entry=number(entry_value);mark=number(mark_value);account=number(account_value) if account_value!='' else None
    with localcontext() as context:
        context.prec=precision_for(account if account is not None else Decimal(0),entry,mark,q)
        context.traps[Inexact]=True
        u=(mark-entry)*q
        m=account-u if account is not None else None
    return [text(q),text(u),text(m) if m is not None else '',('无法计算M：'+'；'.join(reasons)) if reasons else '']
