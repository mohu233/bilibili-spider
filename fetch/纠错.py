import os
from PIL import Image
import json
with open('err.txt','r',encoding='utf-8')as f:
    data = f.readlines()

print(data)
for i in data:
    try:
        i = i[:-1]
        img = Image.open('data/'+i+'/'+i+'.jpg')
        img.save('err/'+i+'.jpg')
        with open('data/' + i + '/summary.json', 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        json_data['是否命中'] = '已核验'
        with open('data/' + i + '/summary.json', 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=4)
    # break
    except:
        print('')