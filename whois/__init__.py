from hoshino import Service  
from hoshino.typing import HoshinoBot, CQEvent  
from hoshino.util import FreqLimiter, filt_message  
  
from .. import chara  
import base64  
from io import BytesIO  
from os.path import dirname, join  
from PIL import Image  
  
# 复用 arena 模块里已填好的 “UNIT ID -> 属性名” 映射，以及 arena/element/ 里的图标  
try:  
    from ..arena.old_main import _UNITID_TO_ELEMENT  
    _ELEMENT_ICON_DIR = join(dirname(dirname(__file__)), 'arena', 'element')  
except Exception:  
    _UNITID_TO_ELEMENT = {}  
    _ELEMENT_ICON_DIR = None  
  
_ELEMENT_ICON_FILES = {  
    "fire": "fire.png", "water": "water.png", "wind": "wind.png",  
    "light": "light.png", "dark": "dark.png",  
}  
  
# whois 头像较大，属性图标也按此尺寸单独加载（不复用 arena 的 20px 小图，避免放大糊）  
_WHOIS_AVATAR_SIZE = 128  
_WHOIS_ELEMENT_SIZE = 40  
  
_WHOIS_ELEMENT_ICONS = {}  
if _ELEMENT_ICON_DIR:  
    for _elem, _fname in _ELEMENT_ICON_FILES.items():  
        try:  
            _p = join(_ELEMENT_ICON_DIR, _fname)  
            _WHOIS_ELEMENT_ICONS[_elem] = Image.open(_p).convert('RGBA').resize(  
                (_WHOIS_ELEMENT_SIZE, _WHOIS_ELEMENT_SIZE), Image.LANCZOS)  
        except Exception:  
            sv.logger.warning(f"whois 缺少属性图标 {_fname}，该属性将不贴图标。")  
sv_help = '''  
[谁是霸瞳] 角色别称查询  
[1001是谁] 通过ID查询角色  
'''.strip()  
  
sv = Service('谁是', help_=sv_help, bundle='pcr查询', enable_on_default=False)  
  
lmt = FreqLimiter(3)  
  
# 基础展示字段（按顺序显示）  
_BASE_FIELDS = ['公会', '生日', '年龄', '身高', '体重', '血型', '种族', '喜好', '声优']  
  
  
def get_chara_profile(chara_id: int) -> str:  
    """根据角色ID从_pcr_data.CHARA_PROFILE获取格式化的档案信息"""  
    try:  
        profile = chara._pcr_data.CHARA_PROFILE.get(chara_id)  
    except AttributeError:  
        return ''  
    if not profile:  
        return ''  
    lines = []  
    # 先按固定顺序展示基础字段  
    for key in _BASE_FIELDS:  
        if key in profile:  
            lines.append(f'{key}: {profile[key]}')  
    # 再展示额外字段（排除"名字"和已展示的基础字段）  
    for key, val in profile.items():  
        if key != '名字' and key not in _BASE_FIELDS:  
            lines.append(f'{key}: {val}')  
    return '\n'.join(lines)  
  
  
def _img2cqcode(img):  
    buf = BytesIO()  
    img.save(buf, format='PNG')  
    b64 = base64.b64encode(buf.getvalue()).decode()  
    return f'[CQ:image,file=base64://{b64}]'  
  
  
async def get_chara_pic(c: chara.Chara):  
    # 渲染头像（兼容新旧 hoshino 的 await 差异）  
    try:  
        icon = await c.render_icon(_WHOIS_AVATAR_SIZE)  
    except TypeError:  
        icon = c.render_icon(_WHOIS_AVATAR_SIZE)  
    icon = icon.convert('RGBA')  
  
    # 右上角叠加属性图标  
    try:  
        elem = _UNITID_TO_ELEMENT.get(int(c.id))  
    except Exception:  
        elem = None  
    elem_icon = _WHOIS_ELEMENT_ICONS.get(elem) if elem else None  
    if elem_icon is not None:  
        icon.paste(elem_icon, (_WHOIS_AVATAR_SIZE - _WHOIS_ELEMENT_SIZE, 0), elem_icon)  
  
    return _img2cqcode(icon)
  
  
@sv.on_suffix('是谁')  
@sv.on_prefix('谁是')  
async def whois(bot: HoshinoBot, ev: CQEvent):  
    name = ev.message.extract_plain_text().strip()  
    if not name:  
        return  
  
    # 支持直接查询ID  
    if name.isdigit():  
        chara_id = int(name)  
        if chara_id in chara.CHARA_NAME:  
            c = chara.fromid(chara_id)  
            profile = get_chara_profile(chara_id)  
            msg = f'{await get_chara_pic(c)}ID: {c.id:04d}\n{" ".join(c.names)}'  
            if profile:  
                msg += f'\n{profile}'  
            await bot.finish(ev, msg.strip())  
        else:  
            await bot.finish(ev, f'没有找到ID为{chara_id}的角色')  
  
    if len(name) > 20:  
        return  
  
    uid: int = ev.user_id  
    if not lmt.check(uid):  
        return  
    lmt.start_cd(uid)  
  
    id_ = chara.name2id(name)  
    if id_ != chara.UNKNOWN:  
        c = chara.fromid(id_)  
        profile = get_chara_profile(id_)  
        msg = f'{await get_chara_pic(c)}ID: {c.id:04d}\n{" ".join(c.names)}'  
        if profile:  
            msg += f'\n{profile}'  
        await bot.finish(ev, msg.strip())  
  
    ids = chara.guess_ids(name)  
    if len(ids) == 0:  
        await bot.finish(ev, f'兰德索尔似乎没有叫"{filt_message(name)}"的人...')  
  
    msg = [  
        f'兰德索尔似乎没有叫"{filt_message(name)}"的人...',  
        "角色别称补全计划: github.com/Ice9Coffee/LandosolRoster",  
        "您要找的可能是："  
    ]  
    for id, name, score in ids:  
        c = chara.fromid(id)  
        entry = f'{await get_chara_pic(c)}ID: {c.id:04d}\n{" ".join(c.names)}'
        profile = get_chara_profile(id)  
        if profile:  
            entry += f'\n{profile}'  
        msg.append(entry.strip())  
  
    await bot.finish(ev, '\n'.join(msg))