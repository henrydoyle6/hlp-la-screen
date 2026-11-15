"""Apply audited derived columns without changing any original CSV cell."""
import argparse,csv,hashlib,json
from pathlib import Path
def main():
 p=argparse.ArgumentParser();p.add_argument('--input-csv',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();base=Path(__file__).resolve().parent
 manifest=json.loads((base/'audit_manifest.json').read_text(encoding='utf-8'))
 with a.input_csv.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==manifest['original_sha256']
 assert not a.output.exists(),'Refusing to overwrite output'
 for item in manifest['support_files']:
  with (base/item['name']).open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==item['sha256']
 summary={r['周期编号']:r for r in csv.DictReader((base/'cycle_audit.csv').open(encoding='utf-8-sig'))}
 patch={r['记录编号']:r for r in csv.DictReader((base/'canonical_values.csv').open(encoding='utf-8-sig'))}
 bad={k for k,v in summary.items() if v['原问题']=='仓位衔接差异'}
 extra=['复核后轨迹完整性','复核后PnL完整性','复核结论','规范化startPosition','规范化数量','规范化成交后仓位','补充官方closedPnl']
 with a.input_csv.open(encoding='utf-8-sig',newline='') as f,a.output.open('x',encoding='utf-8-sig',newline='') as g:
  reader=csv.DictReader(f);writer=csv.DictWriter(g,fieldnames=reader.fieldnames+extra);writer.writeheader()
  for r in reader:
   cid=r['关联主周期编号'];s=summary.get(cid);v=patch.get(r['记录编号'],{})
   r.update(dict(zip(extra,['完整（原始序列核验及表示尾差修复）' if cid in bad else r['轨迹完整性'],'完整' if cid in bad else r['PnL完整性'],s['结论'] if s else '沿用已验证结果',v.get('规范化startPosition',r['原始startPosition']),v.get('规范化数量',r['数量']),v.get('规范化成交后仓位',r['成交后仓位']),''])));writer.writerow(r)
 with a.output.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==manifest['repaired_sha256']
 print('PASS: original cells preserved; repaired file SHA256 matches')
if __name__=='__main__':main()
