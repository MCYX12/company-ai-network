"""Local-only progress signals. Output is a closed vocabulary, never excerpts."""
import re

# Matching requires a subject and an activity in the same sentence.
SUBJECTS = {
 '歌词生成':r'歌词|lyrics', '模型下载':r'下载.{0,24}(?:模型|权重)|(?:模型|权重).{0,24}下载|download.{0,24}(?:model|weight)',
 '模型推理':r'推理|inference', '模型训练':r'训练|fine.?tun|training',
 '依赖环境':r'依赖|虚拟环境|venv|pip install|dependencies',
 '服务部署':r'部署|启动服务|server|deploy', '测试验证':r'测试|pytest|test',
 '文档整理':r'文档|readme|报告', '网页界面':r'前端|网页|界面|frontend',
 '模型转换':r'转换|convert|量化|quantiz',
}
DONE=r'已完成|已经完成|完成了|成功(?:安装|下载|启动|运行|生成|转换)|已(?:安装|下载|生成|修复|实现)|passed|successfully|completed'
BLOCK=r'失败|报错|无法|缺少|不足|阻塞|error|failed|not found|out of memory'
PLAN=r'接下来|下一步|待完成|待办|计划|需要|将要|TODO|next step'

def signals(text,role):
 if role!='assistant':return []
 out=[]
 for sentence in re.split(r'[。！？\n]|(?<=[.!?])\s+',text[:200000]):
  for subject,pattern in SUBJECTS.items():
   if not re.search(pattern,sentence,re.I):continue
   # Negation/plans take precedence: never turn "尚未完成" into completion.
   if re.search(BLOCK,sentence,re.I):state='历史提到问题'
   elif re.search(PLAN+r'|未完成|没有完成|尚未|not completed',sentence,re.I):state='历史计划或未完成'
   elif re.search(DONE,sentence,re.I):state='历史自述已完成'
   else:continue
   value={'subject':subject,'state':state}
   if value not in out:out.append(value)
 return out

def merge(existing,incoming,stamp):
 for entry in incoming:
  value=dict(entry,observed_at=stamp)
  old=next((x for x in existing if x['subject']==value['subject'] and x['state']==value['state']),None)
  if old:
   if stamp>old['observed_at']:old['observed_at']=stamp
  else:existing.append(value)
 return existing
