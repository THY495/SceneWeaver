"""Versioned authoring recipes. Expected answers never enter NPC context."""

from copy import deepcopy

from .narrative_demo import ferry_story

WORLD = "当代雾港私人档案馆。所有人物均为成年人，人物只能依据自己的经历与实际听闻作判断。"


def person(alias, name, core, location, goals, relationships=None, hidden=None):
    return {
        "alias": alias,
        "profile": {
            "name": name,
            "core": core,
            "world": WORLD,
            "relationships": relationships or [],
            "document_ids": [],
        },
        "location": location,
        "goals": goals,
        "unnoticed": hidden or [],
    }


TEMPLATES = {
    "partial-hearing": {
        "schema_version": 1,
        "template_id": "partial-hearing",
        "template_version": 1,
        "title": "雨夜档案馆：部分听闻",
        "description": "核对匿名片段、角色各自的记忆与玩家不在场时的知情边界。",
        "world": WORLD,
        "people": [
            person(
                "curator",
                "林岚",
                "你是馆长，曾在一次交接误会中失去同事的信任。你习惯先确认对方知道多少再解释。你与修复师周澈合作五年，尊重他的手艺，但不愿未经核实就转交机密。",
                "阅览室",
                ["弄清周澈是否听到了刚才的话，再决定如何解释。"],
                [{"target": "周澈", "relation": "长期合作但对保密方式有分歧", "status": "current"}],
                ["restorer"],
            ),
            person(
                "restorer",
                "周澈",
                "你是修复师，曾因交接记录缺失替人承担责任。你重视可核对的来源，不愿仅凭片段指控别人。你与林岚合作五年，仍希望恢复互相信任。",
                "阅览室",
                ["询问刚才听闻片段的含义，不把猜测当成确认信息。"],
                [{"target": "林岚", "relation": "合作五年的馆长", "status": "current"}],
            ),
            person(
                "reporter",
                "许遥",
                "你是来访记者，正在调查旧档案的交接经过。你尚未听到阅览室刚发生的谈话，愿意先向在场者核实。",
                "门厅",
                ["寻找愿意说明交接经过的人。"],
            ),
        ],
        "player": {"alias": "player", "name": "访客", "location": "门厅"},
        "speech": [
            {
                "speaker": "curator",
                "text": "盒子在楼上，钥匙在暗格。",
                "heard": [{"actor": "restorer", "text": "盒子在楼上", "speaker_known": False}],
            }
        ],
    },
    "offstage-dialogue": {
        "schema_version": 1,
        "template_id": "offstage-dialogue",
        "template_version": 1,
        "title": "闭馆前：场外协商",
        "description": "玩家留在门厅，档案室两名NPC带着不同目标自行决定是否交流。",
        "world": WORLD,
        "people": [
            person(
                "archivist",
                "程安",
                "你是档案管理员，负责今晚的交接。此前一次口头约定被误解后，你开始要求明确记录。你愿意听取修复师的理由，但不会代替别人表示同意。",
                "档案室",
                ["向顾宁确认今晚交接是否需要延后，并听取理由。"],
                [{"target": "顾宁", "relation": "经常合作的修复师", "status": "current"}],
            ),
            person(
                "restorer",
                "顾宁",
                "你是修复师，担心匆忙交接会遗漏检查。你与程安都看重档案安全，只是对时间安排有分歧。你会主动提出可讨论的检查安排。",
                "档案室",
                ["向程安提出先核对交接记录再决定时间。"],
                [{"target": "程安", "relation": "谨慎的合作同事", "status": "current"}],
            ),
        ],
        "player": {"alias": "player", "name": "访客", "location": "门厅"},
        "speech": [],
    },
    "reply-lifecycle": {
        "schema_version": 1,
        "template_id": "reply-lifecycle",
        "template_version": 1,
        "title": "交接确认：回应生命周期",
        "description": "从一条明确指定对象的提问开始，对比回应者、旁听者与场外玩家看到的请求状态。",
        "world": WORLD,
        "people": [
            person(
                "clerk",
                "陆葵",
                "你是夜班记录员，必须把交接结论写入日志。你会明确指定问题由谁回答，也接受对方说明需要延后核对。",
                "修复室",
                ["确认沈砚是否同意按核对单完成今晚交接。"],
                [{"target": "沈砚", "relation": "负责同一批档案交接", "status": "current"}],
            ),
            person(
                "technician",
                "沈砚",
                "你是修复技师，已经完成核对单前五项，第六项封套编号仍需亲眼复查。若现在无法确认，你会明确暂缓而不是假装回答；复查有结论后会恢复回应。",
                "修复室",
                ["如实回应陆葵的交接问题，不把未核对项目说成已完成。"],
                [{"target": "陆葵", "relation": "共同负责今晚交接", "status": "current"}],
            ),
            person(
                "witness",
                "闻舟",
                "你是值班主管，在场听到了交接问题，但不会代替沈砚回答。你只记录自己实际听到的回应。",
                "修复室",
                ["观察这次交接是否留下明确记录。"],
            ),
        ],
        "player": {"alias": "player", "name": "访客", "location": "门厅"},
        "speech": [
            {
                "speaker": "clerk",
                "text": "沈砚，你是否同意核对完第六项后，按这份核对单完成今晚交接？",
                "heard": [
                    {
                        "actor": "technician",
                        "text": "沈砚，你是否同意核对完第六项后，按这份核对单完成今晚交接？",
                        "speaker_known": True,
                    },
                    {
                        "actor": "witness",
                        "text": "沈砚，你是否同意核对完第六项后，按这份核对单完成今晚交接？",
                        "speaker_known": True,
                    },
                ],
                "expects_reply": True,
                "target": "technician",
            }
        ],
    },
}


TEMPLATES["ferry-narrative"] = ferry_story()


def template(template_id):
    return deepcopy(TEMPLATES[template_id])
