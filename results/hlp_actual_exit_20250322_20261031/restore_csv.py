"""Losslessly reassemble the published CSV parts; no external dependencies."""
import argparse,hashlib,json,os
from pathlib import Path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',type=Path,default=Path(__file__).resolve().parent);parser.add_argument('--output',type=Path);args=parser.parse_args()
    directory=args.directory.resolve();manifest=json.loads((directory/'parts_manifest.json').read_text(encoding='utf-8'))
    output=args.output or directory/manifest['filename']
    if output.exists():raise FileExistsError(f'Refusing to overwrite {output}')
    temporary=output.with_name(output.name+'.partial');digest=hashlib.sha256()
    with temporary.open('xb') as out:
        for i,item in enumerate(manifest['parts']):
            path=directory/item['filename']
            with path.open('rb') as f:
                assert hashlib.file_digest(f,'sha256').hexdigest()==item['sha256'],f'Part hash mismatch: {path}'
            with path.open('rb') as f:
                header=f.readline()
                assert hashlib.sha256(header).hexdigest()==manifest['header_sha256']
                if i==0:out.write(header);digest.update(header)
                while chunk:=f.read(1024*1024):out.write(chunk);digest.update(chunk)
    assert digest.hexdigest()==manifest['sha256'],'Reassembled file hash mismatch'
    os.replace(temporary,output);print(output)

if __name__=='__main__':main()
