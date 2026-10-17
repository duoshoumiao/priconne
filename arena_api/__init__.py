import asyncio
import base64
import json
import traceback
import re
from io import BytesIO
from aiohttp import web
from PIL import Image

from hoshino import Service
from hoshino.util import pic2b64
from .. import chara

sv = Service('arena_api', help_='竞技场识别HTTP API', bundle='pcr查询')

API_HOST = '0.0.0.0'
API_PORT = 8077
runner = None

CQ_PREFIX = '[CQ:image,file=base64://'
CQ_SUFFIX = ']'

def parse_debug_images(debug_str):
    """从 getPos/getBox 返回的 CQ 码中解析 base64 数据"""
    if not debug_str:
        return None, None
    parts = debug_str.split('\n')
    images = []
    for part in parts:
        part = part.strip()
        if part.startswith(CQ_PREFIX) and part.endswith(CQ_SUFFIX):
            b64_data = part[len(CQ_PREFIX):-len(CQ_SUFFIX)]
            images.append(b64_data)
    highlight = images[0] if len(images) >= 1 else None
    compare = images[1] if len(images) >= 2 else None
    return highlight, compare

async def handle_query_text(request):
    """
    接口 1: 纯文字查询 (POST /api/arena/query_text)
    支持 JSON 或 Form 格式发送 names/text 字段
    """
    try:
        names_list = []
        region = 2
        ids_input = None
        # 兼容性数据提取
        if request.content_type.startswith('multipart/'):
            data = await request.post()
            raw_text = data.get('text') or data.get('names') or ""
            region = int(data.get('region', 2))
            names_list = re.split(r'[,，\s]+', str(raw_text).strip())
        else:
            try:
                body = await request.json()
                names_input = body.get('names') or body.get('text')
                if isinstance(names_input, list):
                    names_list = names_input
                else:
                    names_list = re.split(r'[,，\s]+', str(names_input).strip())
                region = int(body.get('region', 2))
                ids_input = body.get('ids')
            except:
                return web.json_response({"code": -1, "message": "无法解析Body"}, status=400)

        names_list = [n for n in names_list if n]  
        if not names_list and not (isinstance(ids_input, list) and ids_input):  
            return web.json_response({"code": -1, "message": "未检测到角色名"}, status=400)

        # 角色转 ID
        team_ids = []
        for n in names_list:
            cid = chara.name2id(n)
            if cid != 1000:
                team_ids.append(cid // 100 if cid > 10000 else cid)

        # 业务调用  
        from ..arena import arena as arena_module  
        from ..arena.old_main import render_atk_def_teams  
  
        # ★ 客户端直接传 UNIT ID 列表时，跳过 name2id  
        if isinstance(ids_input, list) and ids_input:  
            team_ids = [int(i) for i in ids_input]  
            records = await arena_module.do_query(team_ids, region, 1)  
            if not records:  
                return web.json_response({"code": 1, "message": "未查询到解法", "image": None})  
            img_b64 = None  
            try:  
                teams_img = await render_atk_def_teams(records[:10])  
                buf = BytesIO(); teams_img.save(buf, format='PNG')  
                img_b64 = base64.b64encode(buf.getvalue()).decode()  
            except Exception as e:  
                sv.logger.error(f"ID查询图片渲染失败: {e}")  
            return web.json_response({  
                "code": 0, "message": "success", "image": img_b64,  
                "data": [{"atk": [c.id for c in r["atk"]], "val": r["val"]} for r in records[:10]]  
            })  
  
        records = await arena_module.do_query(team_ids, region, 1)  
        if not records:  
            return web.json_response({"code": 1, "message": "未查询到解法", "image": None})

        # 渲染图片
        img_b64 = None
        try:
            teams_img = await render_atk_def_teams(records[:10])
            buf = BytesIO()
            teams_img.save(buf, format='PNG')
            img_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            sv.logger.error(f"文字查询图片渲染失败: {e}")

        return web.json_response({
            "code": 0,
            "message": "success",
            "image": img_b64,
            "data": [{"atk": [c.id for c in r["atk"]], "val": r["val"]} for r in records[:10]]
        })
    except Exception as e:
        sv.logger.error(f"文字接口报错: {traceback.format_exc()}")
        return web.json_response({"code": -1, "message": str(e)}, status=500)

async def handle_query_image(request):
    """
    接口 2: 图片识别查询 (POST /api/arena/query_image)
    保留原有截图识别、PJJC 无冲配队等逻辑
    """
    try:
        reader = await request.multipart()
        image_data = None
        region = 2
        while True:
            part = await reader.next()
            if part is None: break
            if part.name == 'image': image_data = await part.read()
            elif part.name == 'region': region = int((await part.read()).decode('utf-8'))

        if image_data is None:
            return web.json_response({"code": -1, "message": "缺少image参数"}, status=400)

        from ..arena.old_main import getBox, clear_cache_except_preserved, render_atk_def_teams, recommend1Team, recommend2Teams
        from ..arena import arena as arena_module
        
        clear_cache_except_preserved()
        image = Image.open(BytesIO(image_data))
        box_dict, debug_str = await getBox(image)
        highlight_image, compare_image = parse_debug_images(debug_str)

        if not box_dict:
            return web.json_response({"code": 1, "message": "未识别到阵容", "highlight_image": highlight_image, "compare_image": compare_image})

        box_dict = [team for team in box_dict if len(team) >= 4]
        team_count = len(box_dict)

        if not box_dict:
            return web.json_response({"code": 1, "message": "未识别到4人以上阵容", "highlight_image": highlight_image, "compare_image": compare_image})

        if team_count > 3:
            return web.json_response({"code": 1, "message": "识别队伍过多(>3)", "highlight_image": highlight_image, "compare_image": compare_image})

        # --- 单队 JJC ---
        if team_count == 1:
            team_ids = box_dict[0]
            records = await arena_module.do_query(team_ids, region, 1)
            img_b64 = None
            if records:
                teams_img = await render_atk_def_teams(records[:10])
                buf = BytesIO(); teams_img.save(buf, format='PNG')
                img_b64 = base64.b64encode(buf.getvalue()).decode()
            return web.json_response({"code": 0, "message": "success", "team_count": 1, "image": img_b64, "highlight_image": highlight_image, "compare_image": compare_image})

        # --- 多队 PJJC (2-3队无冲逻辑) ---
        all_query_records = [[] for _ in range(team_count)]
        team_has_result = 0
        for query_index, query_team in enumerate(box_dict):
            all_query_records[query_index].append([None, -100, "placeholder"])
            records = await arena_module.do_query(query_team, region, 1)
            if records:
                team_has_result += 1
                for record in records:
                    record_team = tuple([chara_obj.id for chara_obj in record["atk"]])
                    all_query_records[query_index].append([record_team, record["val"], record])

        if team_has_result == 0:
            return web.json_response({"code": 1, "message": "均无解法", "highlight_image": highlight_image, "compare_image": compare_image})

        # --- 开始无冲计算逻辑 ---
        collision_free_match_cnt = 0
        outp_render = []
        collision_free_match_cnt_2 = 0
        outp_render_2 = []

        if team_count == 2:
            try_combinations = []
            for q1_idx, q1_rec in enumerate(all_query_records[0]):
                for q2_idx, q2_rec in enumerate(all_query_records[1]):
                    val = q1_rec[1] + q2_rec[1]
                    try_combinations.append([q1_idx, q2_idx, val])
            try_combinations = sorted(try_combinations, key=lambda x: x[-1], reverse=True)
            for tc in try_combinations:
                record_1, record_2 = all_query_records[0][tc[0]], all_query_records[1][tc[1]]
                team_1, team_2 = ([] if record_1[0] is None else list(record_1[0])), ([] if record_2[0] is None else list(record_2[0]))
                if len(team_1 + team_2) != len(set(team_1 + team_2)): continue
                val = tc[-1]
                if val < -250: break
                succ = False
                if val < -150:
                    t1, t2 = recommend2Teams(team_1 + team_2)
                    if t1 != "placeholder": record_1[-1], record_2[-1], succ = t1, t2, True
                elif val < -50:
                    t = recommend1Team(team_1 + team_2)
                    if t != "placeholder":
                        if not team_1: record_1[-1] = t
                        if not team_2: record_2[-1] = t
                        succ = True
                else: succ = True
                if succ:
                    collision_free_match_cnt += 1
                    outp_render += [record_1[-1], record_2[-1], []]
                    if collision_free_match_cnt >= 8: break

        if team_count == 3:
            try_combinations = []
            for q1_idx, q1_rec in enumerate(all_query_records[0]):
                for q2_idx, q2_rec in enumerate(all_query_records[1]):
                    for q3_idx, q3_rec in enumerate(all_query_records[2]):
                        try_combinations.append([q1_idx, q2_idx, q3_idx, q1_rec[1] + q2_rec[1] + q3_rec[1]])
            try_combinations = sorted(try_combinations, key=lambda x: x[-1], reverse=True)
            for tc in try_combinations:
                r1, r2, r3 = all_query_records[0][tc[0]], all_query_records[1][tc[1]], all_query_records[2][tc[2]]
                tmix = ([] if r1[0] is None else list(r1[0])) + ([] if r2[0] is None else list(r2[0])) + ([] if r3[0] is None else list(r3[0]))
                if len(tmix) != len(set(tmix)): continue
                val = tc[-1]
                if val < -250: break
                succ = False
                if val < -150:
                    t1, t2 = recommend2Teams(tmix)
                    if t1 != "placeholder":
                        if r1[0]: r2[-1], r3[-1] = t1, t2
                        elif r2[0]: r3[-1], r1[-1] = t1, t2
                        else: r1[-1], r2[-1] = t1, t2
                        succ = True
                elif val < -50:
                    t = recommend1Team(tmix)
                    if t != "placeholder":
                        if not r1[0]: r1[-1] = t
                        if not r2[0]: r2[-1] = t
                        if not r3[0]: r3[-1] = t
                        succ = True
                else: succ = True
                if succ:
                    collision_free_match_cnt += 1
                    outp_render += [r1[-1], r2[-1], r3[-1], []]
                    if collision_free_match_cnt >= 6: break

        # --- 最终图片生成与返回 ---
        img_b64, msg = None, ""
        if collision_free_match_cnt:
            msg = f"找到 {collision_free_match_cnt} 组无冲配队"
            render_list = outp_render[:-1]
        elif collision_free_match_cnt_2:
            msg = f"找到 {collision_free_match_cnt_2} 组部分无冲配队"
            render_list = outp_render_2[:-1]
        else:
            msg = "无冲配对失败，返回单步结果"
            render_list = []
            for records in all_query_records:
                if len(records) > 1: render_list.extend([r[-1] for r in records[1:]])
                render_list.append([])
            if render_list: render_list.pop()

        if render_list:
            try:
                teams_img = await render_atk_def_teams(render_list)
                buf = BytesIO(); teams_img.save(buf, format='PNG')
                img_b64 = base64.b64encode(buf.getvalue()).decode()
            except Exception as e:
                sv.logger.error(f"图片渲染失败: {e}")

        return web.json_response({
            "code": 0, "message": msg, "team_count": team_count, "image": img_b64,
            "highlight_image": highlight_image, "compare_image": compare_image
        })

    except Exception as e:
        sv.logger.error(f"图片接口崩溃: {traceback.format_exc()}")
        return web.json_response({"code": -1, "message": str(e)}, status=500)

async def handle_health(request):
    return web.json_response({"status": "ok"})

app = web.Application(client_max_size=10 * 1024 * 1024)
app.router.add_post('/api/arena/query_image', handle_query_image)
app.router.add_post('/api/arena/query_text', handle_query_text)
app.router.add_get('/api/arena/health', handle_health)

async def _start_api_server():
    global runner
    try:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, API_HOST, API_PORT)
        await site.start()
        sv.logger.info(f'Arena API (文字+图片增强版) 运行于端口: {API_PORT}')
    except Exception as e:
        sv.logger.error(f'API 启动失败: {e}')

loop = asyncio.get_event_loop()
loop.create_task(_start_api_server())