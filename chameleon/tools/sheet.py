import json,sys,copy
sys.path.insert(0,'.')
from engine.resize import resize
from renderer.converter.convert import render_design_to_png
from PIL import Image
name=sys.argv[1]; targets=[tuple(map(int,t.split('x'))) for t in sys.argv[2:]]
src=json.load(open(f'tests/{name}.json'))['source'] if not name.startswith('chameleon') else json.load(open(f'input/{name}.json'))['source']
ims=[]
for i,(w,h) in enumerate(targets):
    out=resize(copy.deepcopy(src),{'width':w,'height':h}); render_design_to_png(out,'brand_kit/assets',f'/tmp/o{i}.png'); ims.append(Image.open(f'/tmp/o{i}.png'))
H=360; rs=[im.resize((max(1,int(im.width*H/im.height)),H)) for im in ims]
W=sum(r.width for r in rs)+10*len(rs); sh=Image.new('RGB',(W,H),'white'); x=0
for r in rs: sh.paste(r,(x,0)); x+=r.width+10
sh.save(f'/home/claude/sheet_{name}.png')
