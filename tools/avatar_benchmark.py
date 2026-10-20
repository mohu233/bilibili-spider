"""Refresh website avatars and compare open classifiers without inventing labels."""
import argparse
import base64
import csv
import hashlib
import html
import io
import json
import random
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEEP_REV = '8fceaaf12270ea680e81fd7984c96fb9abec7523'
SIG_REV = '75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2'
DEEP_MODELS = ['mobilenetv3_v0.1_dist', 'caformer_s36_v0.1']
PROMPTS = [
    ('furry_art', 'An illustration of a furry anthropomorphic animal character with an animal face.'),
    ('fursuit', 'A photograph of a person wearing a furry animal fursuit costume.'),
    ('real_animal', 'A photograph of a real animal or pet.'),
    ('kemonomimi', 'An anime human character with a human face and animal ears.'),
    ('human', 'A photograph or illustration of an ordinary human person.'),
    ('other', 'A logo, text, landscape, object, or other image without a furry character.'),
]

def now():
    return datetime.now(timezone.utc).isoformat()

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

def request(url, headers=None):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache', **(headers or {})})
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=40)

def get_json(url, headers=None):
    with request(url, headers) as r:
        return json.load(r)

def collect(root, database, count):
    from PIL import Image, ImageOps
    manifest_path = root / 'manifest.json'
    if manifest_path.exists():
        manifest = read(manifest_path)
    else:
        with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                'select uid,name,ranking,avatar_url,hit from blog_bilibiliup where uid>0 order by ranking,uid')]
        unique = list({r['uid']: r for r in reversed(rows)}.values())
        unique.sort(key=lambda r: (r['ranking'], r['uid']))
        top = min(20, count, len(unique))
        selected = unique[:top] + random.Random(20261020).sample(unique[top:], min(count-top, len(unique)-top))
        manifest = {'created_at': now(), 'source': str(Path(database).resolve()),
                    'source_type': 'local_website_database_snapshot', 'source_total': len(unique),
                    'sampling': f'top {top} plus seeded random sample; seed=20261020; not population representative',
                    'ground_truth': 'unlabeled; historical hit is NOT a manual label', 'records': selected}
        save(manifest_path, manifest)
    failures = 0
    for i, row in enumerate(manifest['records']):
        if row.get('status') == 'downloaded' and (root / row['image_path']).exists():
            continue
        uid = row['uid']
        try:
            data = get_json(f'https://api.bilibili.com/x/web-interface/card?mid={uid}&photo=0',
                            {'Referer': 'https://www.bilibili.com/'})
            if data.get('code') != 0:
                row.update(status='profile_failed', error_code=data.get('code'), attempted_at=now())
                save(manifest_path, manifest)
                if data.get('code') in (-352, -412, -101):
                    print('Stopped on API restriction:', data.get('code'), flush=True)
                    break
                raise RuntimeError('Profile API unsuccessful')
            card = data['data']['card']
            if int(card['mid']) != int(uid):
                raise ValueError('UID mismatch')
            url = card['face']
            if urllib.parse.urlsplit(url).hostname not in {'i0.hdslb.com', 'i1.hdslb.com', 'i2.hdslb.com'}:
                raise ValueError('Unexpected avatar host')
            with request(url, {'Referer':'https://www.bilibili.com/'}) as response:
                raw = response.read(20_000_001)
            if len(raw) > 20_000_000:
                raise ValueError('Avatar too large')
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                fmt = im.format
                width, height = im.size
                rgba = ImageOps.exif_transpose(im).convert('RGBA')
                bg = Image.new('RGBA', rgba.size, 'white')
                bg.alpha_composite(rgba)
                rgb = bg.convert('RGB')
            ext = {'JPEG':'.jpg','PNG':'.png','WEBP':'.webp','GIF':'.gif'}.get(fmt, '.img')
            rel = f'avatars/{uid}{ext}'
            (root / 'avatars').mkdir(exist_ok=True)
            (root / rel).write_bytes(raw)
            (root / 'thumbnails').mkdir(exist_ok=True)
            rgb.thumbnail((160,160))
            rgb.save(root / 'thumbnails' / f'{uid}.jpg', quality=88)
            row.update(status='downloaded', current_name=card.get('name'), current_avatar_url=url,
                       image_path=rel, sha256=hashlib.sha256(raw).hexdigest(),
                       width=width, height=height, refreshed_at=now(),
                       url_changed=url != row.get('avatar_url'))
            row.pop('error', None)
            failures = 0
        except Exception as e:
            row.update(status='failed', error=type(e).__name__, attempted_at=now())
            failures += 1
        save(manifest_path, manifest)
        print(f'avatars {i+1}/{len(manifest["records"])} uid={uid} status={row["status"]}', flush=True)
        if failures >= 3:
            print('Stopped after three consecutive failures', flush=True)
            break
        time.sleep(1.2)

def download(url, path):
    if path.exists() and path.stat().st_size > 100:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + '.part')
    print('Downloading', path.name, str(path.parent.name), flush=True)
    with request(url) as response, part.open('wb') as stream:
        expected = response.headers.get('Content-Length')
        total = 0
        tick = time.monotonic()
        while chunk := response.read(1024*1024):
            stream.write(chunk)
            total += len(chunk)
            if time.monotonic()-tick > 15:
                print(f'  {total/1024/1024:.0f} MB', flush=True)
                tick = time.monotonic()
    if expected and total != int(expected):
        raise ValueError('Incomplete model download')
    part.replace(path)
    print('Downloaded', path.name, total, flush=True)

def models(root):
    for name in DEEP_MODELS:
        for filename in ['meta.json', 'model.onnx']:
            download(f'https://huggingface.co/deepghs/anime_furry/resolve/{DEEP_REV}/{name}/{filename}',
                     root/'models'/name/filename)
    for filename in ['config.json', 'model.safetensors', 'preprocessor_config.json',
                     'special_tokens_map.json', 'tokenizer.json', 'tokenizer.model', 'tokenizer_config.json']:
        download(f'https://huggingface.co/google/siglip2-base-patch16-224/resolve/{SIG_REV}/{filename}',
                 root/'models'/'siglip2-base-patch16-224'/filename)
    save(root/'model_sources.json', {'downloaded_at':now(), 'deepghs_revision':DEEP_REV,
        'siglip2_revision':SIG_REV, 'deepghs_license':'OpenRAIL (model card)',
        'siglip2_license':'Apache-2.0', 'siglip_prompts':PROMPTS,
        'deepghs_preprocessing':'official imgutils generic.classify: white background, bilinear resize, RGB/255, (x-.5)/.5, NCHW; ONNX output already contains scores'})

def load_rgb(path):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert('RGBA')
        bg = Image.new('RGBA', im.size, 'white')
        bg.alpha_composite(im)
        return bg.convert('RGB')

def infer(root):
    import numpy as np
    import onnxruntime as ort
    from PIL import Image
    rows = [r for r in read(root/'manifest.json')['records'] if r.get('status') == 'downloaded']
    results = read(root/'predictions.json') if (root/'predictions.json').exists() else {}
    for name in DEEP_MODELS:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        sess = ort.InferenceSession(str(root/'models'/name/'model.onnx'), options,
                                    providers=['CPUExecutionProvider'])
        inp = sess.get_inputs()[0]
        _, channels, h, w = inp.shape
        h = h if isinstance(h,int) else 384
        w = w if isinstance(w,int) else 384
        labels = read(root/'models'/name/'meta.json')['labels']
        for i,row in enumerate(rows):
            record = results.setdefault(str(row['uid']), {})
            if name in record:
                continue
            started = time.perf_counter()
            im = load_rgb(root/row['image_path']).resize((w,h), Image.Resampling.BILINEAR)
            data = ((np.asarray(im,dtype=np.float32)/255.0-.5)/.5).transpose(2,0,1)[None]
            output_names = [o.name for o in sess.get_outputs()]
            output = sess.run(output_names, {inp.name:data})[output_names.index('output')][0]
            assert np.isfinite(output).all() and np.all(output>=0) and np.all(output<=1)
            scores = dict(zip(labels, map(float,output)))
            record[name] = {'scores':scores,'furry_score':scores['furry'],
                            'prediction': 'furry' if scores['furry'] >= .5 else 'non_furry',
                            'seconds':time.perf_counter()-started}
            save(root/'predictions.json',results)
            if (i+1)%10==0: print(name,i+1,len(rows),flush=True)
        del sess
    import torch
    from transformers import AutoProcessor, AutoModel
    torch.set_num_threads(4)
    local = root/'models'/'siglip2-base-patch16-224'
    processor = AutoProcessor.from_pretrained(local, local_files_only=True, use_fast=False)
    model = AutoModel.from_pretrained(local, local_files_only=True).eval()
    texts = [p[1] for p in PROMPTS]
    name = 'siglip2'
    for i,row in enumerate(rows):
        record = results.setdefault(str(row['uid']), {})
        if name in record:
            continue
        started = time.perf_counter()
        inputs = processor(text=texts, images=load_rgb(root/row['image_path']),
                           padding='max_length', max_length=64, truncation=True, return_tensors='pt')
        with torch.inference_mode():
            logits = model(**inputs).logits_per_image[0]
            probabilities = logits.sigmoid().tolist()
            relative = logits.softmax(dim=-1).tolist()
        best = int(logits.argmax())
        record[name] = {'scores':dict(zip([p[0] for p in PROMPTS],probabilities)),
                        'relative_furry_score':relative[0]+relative[1],
                        'top_category':PROMPTS[best][0],
                        'prediction':'furry' if best<2 else 'non_furry',
                        'seconds':time.perf_counter()-started,
                        'note':'argmax across six fixed prompts; relative score is NOT calibrated probability'}
        save(root/'predictions.json',results)
        if (i+1)%10==0: print(name,i+1,len(rows),flush=True)

def report(root):
    manifest = read(root/'manifest.json')
    predictions = read(root/'predictions.json') if (root/'predictions.json').exists() else {}
    rows = [r for r in manifest['records'] if r.get('status') == 'downloaded']
    data=[]
    for row in rows:
        uid=str(row['uid'])
        thumb=base64.b64encode((root/'thumbnails'/f'{uid}.jpg').read_bytes()).decode()
        data.append({'uid':uid,'name':row.get('current_name',row['name']),
                     'image':'data:image/jpeg;base64,'+thumb,'original':row['image_path'],
                     'predictions':predictions.get(uid,{})})
    payload=json.dumps(data,ensure_ascii=False).replace('<','\\u003c')
    page='''<!doctype html><meta charset="utf-8"><title>头像模型对比与人工标注</title>
<style>body{font:15px system-ui;background:#f4f6f2;color:#24352b;margin:24px}header{position:sticky;top:0;background:#f4f6f2;padding:12px;z-index:1}button,select{padding:8px;margin:4px}#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}.card{background:white;padding:14px;border-radius:12px}.card img{width:140px;height:140px;object-fit:contain}.scores{display:none;font:12px monospace;white-space:pre-wrap}body.show .scores{display:block}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #bbc7bd}small{color:#56645a}</style>
<header><h1>新下载头像 · 模型对比与人工标注</h1><p>先看图片标注，再查看预测，减少模型提示造成的偏差。这里只判断头像内容，不判断账号身份。</p>
<p>建议口径：兽设插画、兽装算 Furry；普通真实动物、仅兽耳人形不算。模糊图片选“不确定”。当前样本来自本地网站榜单快照，不能代表全部 B 站头像。</p>
<button onclick="document.body.classList.toggle('show')">显示 / 隐藏模型预测</button><button onclick="exportLabels()">导出人工标签 JSON</button><label>导入标签 <input type="file" accept=".json" onchange="importLabels(this)"></label>
<select id="filter" onchange="render()"><option value="all">全部图片</option><option value="disagree">模型分歧</option><option value="unlabeled">待标注</option></select>
<div id="stats"></div><div id="metrics"></div></header><div id="grid"></div>
<script>const data=PAYLOAD;const names=['mobilenetv3_v0.1_dist','caformer_s36_v0.1','siglip2'];const key='furry-avatar-labels-20261020';let labels={};try{labels=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function setLabel(uid,value){if(value)labels[uid]=value;else delete labels[uid];try{localStorage.setItem(key,JSON.stringify(labels))}catch(e){}metrics()}
function disagree(r){let p=Object.values(r.predictions).map(p=>p.prediction);return new Set(p).size>1}
function render(){let filter=document.getElementById('filter').value;document.getElementById('grid').innerHTML=data.filter(r=>filter==='all'||filter==='disagree'&&disagree(r)||filter==='unlabeled'&&!labels[r.uid]).map(r=>`<article class="card"><a href="${esc(r.original)}" target="_blank" title="查看下载的原图"><img src="${r.image}" alt="头像，点击查看原图"></a><h3>${esc(r.name)}</h3><small>UID ${r.uid} ${disagree(r)?'· 模型分歧':''}</small><p><select onchange="setLabel('${r.uid}',this.value)">${[['','未标注'],['furry','Furry'],['non_furry','非 Furry'],['uncertain','不确定']].map(([v,t])=>`<option value="${v}" ${labels[r.uid]===v?'selected':''}>${t}</option>`).join('')}</select></p><div class="scores">${names.map(n=>{let p=r.predictions[n];return esc(n)+': '+(p?esc(p.prediction)+' '+Number(p.furry_score??p.relative_furry_score).toFixed(4)+(p.top_category?' ['+esc(p.top_category)+']':''):'未完成')}).join('<br>')}</div></article>`).join('');metrics()}
function metrics(){let n=data.filter(r=>['furry','non_furry'].includes(labels[r.uid])).length;document.getElementById('stats').textContent=`下载 ${data.length} 张；人工有效标注 ${n} 张；模型分歧 ${data.filter(disagree).length} 张。DeepGHS 显示分类分数；SigLIP 显示固定候选描述下的相对分数，数值不可直接比较。`;
if(!n){document.getElementById('metrics').textContent='暂无人工标签，不能计算准确率。';return}let pct=(a,b)=>b?(100*a/b).toFixed(1)+'%':'—';document.getElementById('metrics').innerHTML='<table><tr><th>模型</th><th>已评估</th><th>准确率</th><th>Precision</th><th>Recall</th><th>TP / FP / TN / FN</th></tr>'+names.map(name=>{let tp=0,fp=0,tn=0,fn=0;data.forEach(r=>{let truth=labels[r.uid],p=r.predictions[name];if(!p||!['furry','non_furry'].includes(truth))return;if(truth==='furry'){if(p.prediction==='furry')tp++;else fn++}else{if(p.prediction==='furry')fp++;else tn++}});let count=tp+fp+tn+fn;return `<tr><td>${name}</td><td>${count}</td><td>${pct(tp+tn,count)}</td><td>${pct(tp,tp+fp)}</td><td>${pct(tp,tp+fn)}</td><td>${tp} / ${fp} / ${tn} / ${fn}</td></tr>`}).join('')+'</table><small>仅针对已人工标注且推理成功的样本；不是总体准确率。分歧筛选后的标签不能代表整批样本。</small>'}
function exportLabels(){let blob=new Blob([JSON.stringify({schema_version:1,created_at:new Date().toISOString(),source:'manual_user_review',labels},null,2)],{type:'application/json'});let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='manual_labels.json';a.click();URL.revokeObjectURL(a.href)}
async function importLabels(input){let d=JSON.parse(await input.files[0].text());let incoming=d.labels||d;let allowed=new Set(data.map(r=>r.uid));for(let [uid,label]of Object.entries(incoming)){if(allowed.has(uid)&&['furry','non_furry','uncertain'].includes(label))labels[uid]=label}try{localStorage.setItem(key,JSON.stringify(labels))}catch(e){}render()}render();</script>'''.replace('PAYLOAD',payload)
    (root/'review.html').write_text(page,encoding='utf-8')
    with (root/'predictions.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['uid','name','image_path','model','prediction','furry_score_or_relative','top_category','manual_label'])
        for row in rows:
            for name,p in predictions.get(str(row['uid']),{}).items():
                writer.writerow([row['uid'],row.get('current_name',row['name']),row['image_path'],name,p['prediction'],p.get('furry_score',p.get('relative_furry_score')),p.get('top_category',''),''])
    from PIL import Image,ImageDraw,ImageFont
    font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',14)
    for page_num,start in enumerate(range(0,len(rows),25),1):
        sheet=Image.new('RGB',(1000,1100),'white');draw=ImageDraw.Draw(sheet)
        for j,row in enumerate(rows[start:start+25]):
            x=(j%5)*200;y=(j//5)*220
            im=load_rgb(root/row['image_path']);im.thumbnail((170,170))
            sheet.paste(im,(x+(200-im.width)//2,y))
            draw.text((x+8,y+173),str(row['uid']),font=font,fill='black')
            draw.text((x+8,y+195),row.get('current_name',row['name'])[:11],font=font,fill='black')
        sheet.save(root/f'contact_sheet_{page_num}.jpg',quality=90)
    lines=['# 头像模型对比实验','',f'- 生成时间：{now()}',
           f'- UID 来源：`{manifest["source"]}`（网站本地数据库快照，共 {manifest["source_total"]} 个 UID）；线上接口受 Cloudflare 限制，未宣称使用线上实时榜单。',
           '- 抽样：前 20 名 + 固定随机种子抽取 80 名；不是全站无偏样本。',
           '- 每张头像均重新查询 B 站当前头像地址并发起下载，未复用榜单旧图片文件。',
           f'- 已下载：{len(rows)} / {len(manifest["records"])}；地址变化：{sum(bool(r.get("url_changed")) for r in rows)}。',
           '- 人工标签：尚未提供。历史 hit 字段和模型一致结果均不作为正确答案。',
           '- 准确率、Precision、Recall：待人工标注后计算，当前不提供虚构数值。','',
           '| 模型 | 完成推理 | 判 Furry | 判非 Furry | 平均单张推理秒数（含预处理，不含模型加载） |','| --- | ---: | ---: | ---: | ---: |']
    for name in DEEP_MODELS+['siglip2']:
        ps=[p[name] for p in predictions.values() if name in p]
        pos=sum(p['prediction']=='furry' for p in ps)
        mean=sum(p['seconds'] for p in ps)/len(ps) if ps else 0
        lines.append(f'| {name} | {len(ps)} | {pos} | {len(ps)-pos} | {mean:.3f} |')
    complete=[p for p in predictions.values() if all(n in p for n in DEEP_MODELS+['siglip2'])]
    conflicts=sum(len({p[n]['prediction'] for n in DEEP_MODELS+['siglip2']})>1 for p in complete)
    unique_images=len({r['sha256'] for r in rows})
    lines += ['',f'- 三个模型全部完成的样本：{len(complete)}；存在二分类分歧的样本：{conflicts}。',
              f'- 按 SHA-256 计有 {unique_images} 张不同图片；相同图片对应多个 UID 时，后续评估应同时报告去重结果，避免重复样本影响指标。']
    lines += ['', '## 使用','',
        '打开 `review.html`，先人工标注头像，再切换显示模型结果；页面会按有效人工标签计算各模型准确率、Precision、Recall 和混淆矩阵。',
        '标签默认保存在当前浏览器；标注后点击“导出人工标签 JSON”保存，必要时重新导入。未标注和不确定项不参与准确率计算。',
        '若浏览器禁用本地存储，导出 JSON 仍可备份标签。预览内嵌本地缩略图，不会把图片上传至第三方。','',
        '## 方法与限制','',
        '- DeepGHS：采用官方预处理（白底 RGB、双线性缩放、归一化到 [-1,1]），读取 ONNX output，furry 分数 ≥ 0.5 判为 Furry。',
        '- SigLIP 2：六个固定英文候选描述，最高项为兽设插画或兽装时判 Furry；提示词详见 model_sources.json。相对分数不是校准概率，不能直接与 DeepGHS 分数比较。',
        '- 模型版本固定到下载时的仓库提交，见 model_sources.json；不直接执行远程自定义代码。',
        '- 头像中新出现的人工标签必须独立于旧头像、简介或榜单身份，不能沿用旧 hit 当真值。',
        '- 本批榜单样本偏向已有候选，不能代表普通用户总体，也不能用于宣称模型在全站的准确率。',
        '- 本实验未修改网站数据库、榜单或现有模型配置。','']
    (root/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print('Report:',root/'review.html',flush=True)

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['collect','models','infer','report'])
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--database',default='E:/web/db.sqlite3')
    parser.add_argument('--count',type=int,default=100)
    args=parser.parse_args()
    args.root.mkdir(parents=True,exist_ok=True)
    if args.mode=='collect':collect(args.root,args.database,args.count)
    elif args.mode=='models':models(args.root)
    elif args.mode=='infer':infer(args.root)
    else:report(args.root)
