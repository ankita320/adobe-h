"""Dev-only checker: runs the engine over many (design, target) pairs and reports
structural problems, text overflow (using the renderer's own wrapping) and collisions."""
import json, sys, glob, math, itertools, copy
sys.path.insert(0, '.')
from engine.resize import resize
from engine.main import self_check
from renderer.converter.convert import convert_chameleon_to_crello, _get_renderer

R = _get_renderer('brand_kit/fonts')
TARGETS = {
 'Instagram Post': (1080,1080), 'Instagram Story': (1080,1920), 'Portrait 4:5': (1080,1350),
 'Facebook Cover': (820,312), 'YouTube Thumbnail': (1280,720), 'Twitter Banner': (1500,500),
 'Leaderboard': (728,90), 'Skyscraper': (160,600), 'Medium Rectangle': (300,250),
 'LinkedIn Banner': (1584,396), 'A4 Poster': (1240,1754), 'Wide Banner': (1600,400), 'Billboard': (970,250),
}
def rot_bbox(e):
    x,y,w,h=e['x'],e['y'],e['width'],e['height']; d=e.get('rotation',0) or 0
    if not d: return x,y,w,h
    r=math.radians(d); c,s=abs(math.cos(r)),abs(math.sin(r)); bw,bh=w*c+h*s,w*s+h*c
    cx,cy=x+w/2,y+h/2; return cx-bw/2,cy-bh/2,bw,bh
def ov(a,b):
    ix=min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]); iy=min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1])
    return max(0,ix)*max(0,iy)
def check(src,out):
    probs=[]
    probs+=self_check(src,out)
    sw,sh=src['canvas']['width'],src['canvas']['height']
    S={e['id']:e for e in src['elements']}
    ex,_=convert_chameleon_to_crello(out,R.font_manager,'brand_kit/assets')
    els=sorted(out['elements'],key=lambda e:e.get('z_index',0))
    for e,t,f in zip(els,ex['text'],ex['font_size']):
        s=S[e['id']]
        if e['type']=='image' and s['width']*s['height']<0.85*sw*sh:
            a0=s['width']/s['height']; a1=e['width']/max(e['height'],1e-6)
            if abs(a1/a0-1)>0.05: probs.append(f"aspect {e['id']} {a0:.2f}->{a1:.2f}")
        if e['type']=='text' and e['font_size']>=3:
            n=t.count('\n')+1; need=n*e['font_size']*e.get('line_height',1.2)
            if need>e['height']*1.03+1: probs.append(f"overflow {e['id']} need {need:.0f} have {e['height']:.0f} (n={n})")
            if e['font_size']<8: probs.append(f"tiny {e['id']} {e['font_size']}")
    big=[e for e in els if e['width']*e['height']>=0.85*out['canvas']['width']*out['canvas']['height']]
    grp={}
    for e in els: grp.setdefault(e.get('group_id') or e['id'],[]).append(e)
    for a,b in itertools.combinations(els,2):
        if a in big or b in big: continue
        if (a.get('group_id') and a.get('group_id')==b.get('group_id')): continue
        if a['font_size'] if a['type']=='text' else 1: pass
        if (a['type']=='text' and a['font_size']<3) or (b['type']=='text' and b['font_size']<3) or a['width']<6 or b['width']<6: continue
        if a['type']=='shape' and a.get('opacity',1)<0.7 or b['type']=='shape' and b.get('opacity',1)<0.7: continue
        # a panel shape that contains the other is intentional
        A,B=rot_bbox(a),rot_bbox(b); o=ov(A,B)
        if o>=0.95*min(A[2]*A[3],B[2]*B[3]): continue  # one fully contains the other = intentional layering
        if o>0.02*min(A[2]*A[3],B[2]*B[3]) and o>20: probs.append(f"collide {a['id']} x {b['id']} ({o:.0f}px2)")
    return probs
if __name__=='__main__':
    files=sorted(glob.glob('input/*.json'))+sorted(glob.glob('tests/*.json'))
    bad=0; tot=0
    for f in files:
        task=json.load(open(f)); src=task['source']
        for name,(w,h) in TARGETS.items():
            out=resize(copy.deepcopy(src),{'width':w,'height':h}); tot+=1
            p=check(src,out)
            if p:
                bad+=1; print(f"[{f.split('/')[-1][:28]}] -> {name} {w}x{h}: "+'; '.join(p[:4]))
    print(f"{tot-bad}/{tot} clean")
