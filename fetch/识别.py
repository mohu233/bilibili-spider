import time

from ultralytics import YOLO
import cv2
import json
# model = YOLO('furry_yolo.pt')
import os
os.environ['CUDA_LAUNCH_BLOCKING'] = '1'
headers = {}
with open('headers.txt', 'r') as file:
    for line in file:
        line = line.strip()
        if not line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip()
        headers[key] = value


def get_all_folders(directory):
    # 获取目录中的所有文件和文件夹
    items = os.listdir(directory)
    # 筛选出文件夹
    folders = [item for item in items if os.path.isdir(os.path.join(directory, item))]
    return folders


# 示例使用

def aa():
    directory_path = os.getcwd() + '/data'
    folders = get_all_folders(directory_path)
    for i in folders:
        print(i)
        with open(directory_path+'/'+i+'/summary.json','r', encoding='utf-8') as file:
            json_data = json.load(file)
        json_data['是否命中'] = False
        json_data['命中规则']['头像'] = False
        json_data['命中规则']['简介'] = False

        with open(directory_path + '/' + i + '/summary.json', 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=4)
def bb():
    num = 0
    a = 0
    b = 0
    c = 0
    d = 0
    key = 0.5
    str_key = ['福瑞', '兽设', 'Furry', 'furry', 'FURRY', 'Fursuit', 'fursuit', 'FURSUIT', '兽装', '兽兽','兽聚','装师']
    directory_path = os.getcwd() + '/data'
    folders = get_all_folders(directory_path)
    # model = YOLO('furry_yolo.pt')
    model = YOLO('furry1500x200.pt')
    for i in folders:
        try:
            # print('查验',i)
            with open(directory_path+'/'+i+'/summary.json','r', encoding='utf-8') as file:
                json_data = json.load(file)

                if json_data['是否命中'] == True:
                    num += 1
                    if json_data['遍历']:
                        b+=1
                    try:
                        with open(directory_path + '/' + i + '/fans.json', 'r', encoding='utf-8') as file:
                            tamp = json.load(file)
                        c += 1
                    except:
                        print('未找到fans')
                        d += 1
                    continue
                elif json_data['是否命中'] == '已核验':
                    # print('跳过')
                    a += 1
                else:
                    print('查验',i)
                    if cc(directory_path ,i,key,model,str_key):
                        num += 1
        except:
            print('文件损坏')
    print('找到{}个福瑞，跳过{}个,遍历{}个,已下载数据{}个，未下载{}个'.format(num,a,b,c,d))

def cc(directory_path ,i,key,model,str_key):
    print(i)
    image = cv2.imread(directory_path + '/' + i + '/' + i + '.jpg', cv2.IMREAD_COLOR)
    # cv2.imshow('Loaded Image', image)
    with open(directory_path + '/' + i + '/summary.json', 'r', encoding='utf-8') as file:
        json_data = json.load(file)
    with open(directory_path + '/' + i + '/data.json', 'r', encoding='utf-8') as file:
        json_sign = json.load(file)

    json_data['是否命中'] = '已核验'
    sign = json_sign['sign']
    print(sign)
    for j in str_key:
        # print(j)
        if j in sign:
            json_data['是否命中'] = True
            json_data['命中规则']['简介'] = True
    # json_data = json.loads(json_data)
    results = model(image)
    # print(type(results))
    # print(results)
    # print('results:', type(results[0].boxes.cls))
    results_1 = results[0].boxes.cls.tolist()
    results_2 = results[0].boxes.conf.tolist()
    # print('results:',results[0].boxes.cls.tolist())
    # print('results:', results[0].boxes.conf.tolist())
    # print(results_1[0])
    # print(results_2[0])
    # print(type(results_1[0]))
    # print(len(results_1))
    if results_1 == []:
        pass
    else:
        for j in range(len(results_1)):
            print(results_2[j])
            print(results_1[j])
            if results_1[j] == 0 and results_2[j] > key:
                json_data['是否命中'] = True
                json_data['命中规则']['头像'] = True
            elif results_1[j] == 1 and results_2[j] > key:
                json_data['是否命中'] = True
                json_data['命中规则']['头像'] = True
    print(json_data)

    with open(directory_path + '/' + i + '/summary.json', 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=4)

    # print('-------------------')
    annotated_frame = results[0].plot()

    if json_data['是否命中']:
        # cv2.imwrite(os.getcwd() + '/tamp/' + i + '_.jpg', annotated_frame)
        # print(annotated_frame)
        cv2.imwrite(directory_path + '/' + i + '/' + i + '_.jpg', annotated_frame)
        return True
    else:
        return False

# aa()
# directory_path = os.getcwd() + '/data'
# model = YOLO('furryx80neg.pt')
# key = 0.4
# str_key = ['福瑞','兽设','Furry','furry','FURRY','Fursuit','fursuit','FURSUIT','兽装','兽兽']
# i = '43728345'
# cc(directory_path ,i,key,model,str_key)

# if '已核验' == True:
#     print(1)


while True:
    bb()
    time.sleep(30)


# num  = 0
# directory_path = 'data/697451963'
# with open(directory_path+'/summary.json','r', encoding='utf-8') as file:
#     json_data = json.load(file)
#
#     if json_data['是否命中'] == True:
#         num += 1
#     elif json_data['是否命中'] == '已核验':
#         print('跳过')
#     else:
#         if cc(directory_path ,i,key,model,str_key):
#             num += 1
# print(num)