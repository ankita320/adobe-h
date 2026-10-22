"""Generates hand-made source designs (different structures than the 3 samples) into tests/."""
import json
BG='solstice_bg_gradient.png'; BOT='solstice_bottle.png'; LOGO='solstice_logo_lockup.png'
def img(i,a,x,y,w,h,z,fit='contain'): return dict(id=i,type='image',asset_id=a,x=x,y=y,width=w,height=h,z_index=z,fit=fit)
def txt(i,c,x,y,w,h,z,fs,fam='Solstice Sans',col='rgba(255,255,255,1)',al='center',bold=False,**k):
    d=dict(id=i,type='text',content=c,x=x,y=y,width=w,height=h,z_index=z,font_family=fam,font_size=fs,color=col,text_align=al)
    if bold:d['font_weight']='bold'
    d.update(k);return d
def shp(i,x,y,w,h,z,fill,t='rectangle',**k):
    d=dict(id=i,type='shape',shape_type=t,x=x,y=y,width=w,height=h,z_index=z,fill_color=fill);d.update(k);return d
def save(name,sw,sh,els):
    json.dump({'source':{'canvas':{'width':sw,'height':sh},'elements':els},'target_canvas':{'width':1080,'height':1080}},open(f'tests/{name}.json','w'),indent=1)

# A: wide sale banner -> portrait etc.
save('t_wide_banner',1600,400,[
 img('bg',BG,0,0,1600,400,0,'cover'),img('logo',LOGO,40,40,260,90,3),
 txt('headline','Summer Sale: Up to 40% Off',360,60,700,150,3,64,'Solstice Serif',bold=True,al='left'),
 txt('subhead','Cold-pressed botanicals, sparkling water, zero sugar.',360,230,700,90,3,26,al='left',col='rgba(255,255,255,0.85)'),
 shp('cta_bg',1120,150,360,100,3,'rgba(232,106,58,1)',corner_radius=50,group_id='cta'),
 txt('cta_label','Shop Now',1120,178,360,44,4,30,bold=True,group_id='cta'),
 img('product',BOT,1250,10,300,300,2)])
# B: panel + overlay text on product + divider
save('t_card_poster',1080,1920,[
 img('bg',BG,0,0,1080,1920,0,'cover'),img('logo',LOGO,380,80,320,100,3),
 img('product',BOT,240,260,600,600,2),
 txt('label','New',440,520,200,50,4,36,bold=True,col='rgba(43,27,46,1)'),
 shp('panel',80,940,920,640,1,'rgba(43,27,46,0.95)',corner_radius=30),
 txt('headline','Golden Hour in a Bottle',120,980,840,200,3,72,'Solstice Serif',bold=True),
 shp('divider',140,1210,800,4,3,'rgba(232,106,58,1)'),
 txt('body','Wild honey, blood orange and smoked rosemary, sparkling and bright.',120,1240,840,160,3,34,col='rgba(255,255,255,0.9)'),
 shp('btn_bg',300,1700,480,110,3,'rgba(232,106,58,1)',corner_radius=55,group_id='btn'),
 txt('btn_label','Learn More',300,1730,480,50,4,34,bold=True,group_id='btn')])
# C: left aligned, no CTA, logo at bottom
save('t_left_aligned',1080,1080,[
 img('bg',BG,0,0,1080,1080,0,'cover'),
 txt('headline','Refresh your afternoon',80,120,560,260,3,84,'Solstice Serif',bold=True,al='left'),
 txt('body','Light, floral and never too sweet.',80,420,520,120,3,34,al='left',col='rgba(255,255,255,0.85)'),
 img('product',BOT,640,200,380,640,2),img('logo',LOGO,80,900,300,100,3)])
# D: text only, one very long word
save('t_text_only',1080,1350,[
 img('bg',BG,0,0,1080,1350,0,'cover'),
 txt('t1','Supercalifragilisticexpialidocious Savings',90,100,900,260,3,80,'Solstice Serif',bold=True),
 txt('t2','Limited time only',90,400,900,80,3,44),
 txt('t3','Terms and conditions apply. Offer valid while supplies last in participating locations.',90,520,900,140,3,28,col='rgba(255,255,255,0.7)'),
 txt('t4','solstice.com/summer',90,700,900,60,3,36,bold=True,col='rgba(240,180,60,1)'),
 txt('t5','Follow us',90,800,900,50,3,30),txt('t6','@solstice',90,860,900,50,3,30)])
# E: many badges, ribbon, deco
els=[img('bg',BG,0,0,1080,1080,0,'cover'),
 shp('d1',-150,-150,500,500,1,'rgba(255,255,255,1)','ellipse',opacity=0.12),
 shp('d2',800,820,350,350,1,'rgba(255,255,255,1)','ellipse',opacity=0.1),
 img('logo',LOGO,380,50,320,100,4),
 txt('headline','Five Reasons to Love Solstice',90,200,900,200,4,70,'Solstice Serif',bold=True)]
labs=['ORGANIC','VEGAN','NO SUGAR','LOCAL','RECYCLED','FAIR TRADE']
for i,l in enumerate(labs):
    x=60+(i%3)*330;y=480+(i//3)*90
    els+= [shp(f'b{i}_bg',x,y,300,64,3,'rgba(232,106,58,1)',corner_radius=32,group_id=f'b{i}'),txt(f'b{i}_l',l,x,y+16,300,32,4,22,bold=True,group_id=f'b{i}')]
els+=[shp('rib_bg',700,880,320,60,4,'rgba(220,60,50,1)',rotation=-8,group_id='rib'),txt('rib_l','SALE ENDS SOON',700,893,320,34,5,24,bold=True,rotation=-8,group_id='rib'),
 shp('cta_bg',330,900,420,100,4,'rgba(232,106,58,1)',corner_radius=50,group_id='cta'),txt('cta_l','Get Yours Today',330,928,420,44,5,30,bold=True,group_id='cta')]
save('t_many_badges',1080,1080,els)
# F: solid colour background shape, two products
save('t_solid_bg',1080,1080,[
 shp('bgrect',0,0,1080,1080,0,'rgba(43,27,46,1)'),img('logo',LOGO,60,50,260,90,3),
 img('p1',BOT,120,250,380,380,2),img('p2',BOT,580,250,380,380,2),
 txt('headline','Two flavors. One golden hour.',90,700,900,170,3,64,'Solstice Serif',bold=True),
 txt('cta','Shop the Summer Line',90,900,900,60,3,36,bold=True,col='rgba(232,106,58,1)')])
# G: skyscraper source
save('t_skyscraper_src',300,600,[
 img('bg',BG,0,0,300,600,0,'cover'),img('logo',LOGO,60,20,180,60,3),img('product',BOT,60,110,180,180,2),
 txt('headline','Chase the Light',20,320,260,90,3,36,'Solstice Serif',bold=True),
 txt('sub','Botanical sparkling infusions.',20,420,260,60,3,16,col='rgba(255,255,255,0.85)'),
 shp('cta_bg',50,510,200,50,3,'rgba(232,106,58,1)',corner_radius=25,group_id='c'),txt('cta_l','Shop Now',50,523,200,24,4,16,bold=True,group_id='c')])
# H: light background shape + dark text (contrast case)
save('t_light_bg',1080,1350,[
 shp('bgrect',0,0,1080,1350,0,'rgba(245,238,225,1)'),img('logo',LOGO,380,60,320,100,3),
 txt('headline','Fresh. Local. Sparkling.',90,300,900,200,3,80,'Solstice Serif',bold=True,col='rgba(43,27,46,1)'),
 img('product',BOT,290,560,500,500,2),
 txt('sub','Botanical infusions for every season.',90,1100,900,80,3,36,col='rgba(43,27,46,0.9)')])
