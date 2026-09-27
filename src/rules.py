from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# 舱级转驳
SHIP_ENTITY='沉船'; TANK_ENTITY='沉船舱'; SHORE_TANK_ENTITY='岸罐'; TRANSFER_ENTITY='转驳作业'
TRANSFER_STATES=['planned','held','in_progress','reconciling','confirmed']
# 作业未结：除confirmed且确认未作废外都算占用船舱
TRANSFER_OPEN_STATES=set(TRANSFER_STATES[:-1])
TRANSFER_FAIL_ITEMS=['remaining_capacity','grounding_resistance','wind_speed']
FAIL_ITEM_LABELS={'remaining_capacity':'剩余舱容','grounding_resistance':'接地电阻','wind_speed':'风速'}
# 开工前安全核对限值：接地电阻100Ω、风速10.8m/s（六级风上限）
MAX_GROUNDING_RESISTANCE=100.0
MAX_WIND_SPEED=10.8
# 船端交油与岸端接收计量差阈值：2%
MEASURE_DIFF_LIMIT=0.02
TRANSFER_CREATE_ROLES=set(['operations'])
TRANSFER_PRECHECK_ROLES=set(['operations'])
TRANSFER_MEASURE_ROLES=set(['operations'])
TRANSFER_CONFIRM_ROLES=set(['response_commander'])
SHIP_ROLES=set(['observer','response_commander','operations'])
TANK_PLAN_ROLES=set(['response_commander'])

def precheck_failures(remaining_capacity, planned_qty, grounding_resistance, wind_speed):
    failures=[]
    if remaining_capacity < planned_qty:
        failures.append('remaining_capacity')
    if grounding_resistance > MAX_GROUNDING_RESISTANCE:
        failures.append('grounding_resistance')
    if wind_speed > MAX_WIND_SPEED:
        failures.append('wind_speed')
    return failures

def measure_diff_ratio(ship_qty, shore_qty):
    base=max(ship_qty, shore_qty)
    if base<=0: return 0.0
    return abs(ship_qty-shore_qty)/base

def measure_over_limit(ship_qty, shore_qty):
    return measure_diff_ratio(ship_qty, shore_qty) > MEASURE_DIFF_LIMIT

def transfer_gap(transfer):
    """单个转驳作业在进入回收前的未结缺口，无缺口返回None"""
    status=transfer['status']
    ident=f"{transfer['ship_name']}/{transfer['tank_name']}#{transfer['id']}"
    if status=='confirmed' and transfer['confirmation_voided']:
        return {'transfer_id':transfer['id'],'ship':transfer['ship_name'],
                'tank':transfer['tank_name'],'code':'confirmation_voided',
                'message':f"作业{ident}舱容计划已修订，原确认作废，需重新确认"}
    if status=='planned':
        return {'transfer_id':transfer['id'],'ship':transfer['ship_name'],
                'tank':transfer['tank_name'],'code':'not_started',
                'message':f"作业{ident}尚未开工核对"}
    if status=='held':
        items='、'.join(FAIL_ITEM_LABELS[i] for i in transfer['failed_items']) or '安全核对'
        return {'transfer_id':transfer['id'],'ship':transfer['ship_name'],
                'tank':transfer['tank_name'],'code':'held',
                'message':f"作业{ident}停待处理：{items}不符"}
    if status=='in_progress':
        return {'transfer_id':transfer['id'],'ship':transfer['ship_name'],
                'tank':transfer['tank_name'],'code':'not_measured',
                'message':f"作业{ident}未提交船岸计量"}
    if status=='reconciling':
        ratio=transfer['diff_ratio'] if transfer['diff_ratio'] is not None else 0.0
        return {'transfer_id':transfer['id'],'ship':transfer['ship_name'],
                'tank':transfer['tank_name'],'code':'discrepancy',
                'diff_ratio':round(ratio,4),
                'message':f"作业{ident}船岸计量差{ratio*100:.2f}%超过2%，待核对补齐"}
    return None

def recovery_gaps(transfers):
    gaps=[]
    for transfer in transfers:
        gap=transfer_gap(transfer)
        if gap is not None: gaps.append(gap)
    return gaps
