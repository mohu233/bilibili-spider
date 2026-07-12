import time
from nt import times
import requests as r
import  traceback
import os
import json
from datetime import datetime
import chardet

time_ = 1.2
headers = {}
with open('headers.txt', 'r') as file:
    for line in file:
        line = line.strip()
        if not line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip()
        if key == 'User-Agent' or key == 'Cookie':
            headers[key] = value
print(headers)


def add_dir(directory_path):
    try:
        os.makedirs('data/'+directory_path)
        print(f"目录 '{directory_path}' 已创建。")
        return 'data/' + directory_path
    except OSError as e:
        # traceback.print_exc()
        print(directory_path,'文件存在')
        return 0


def dow_fans(mid):
    for i in range(1,6):
        url = 'https://api.bilibili.com/x/relation/fans?pn='+str(i)+'&ps=24&vmid='+mid+'&gaia_source=main_web&web_location=333.1387'
        print(url)
        # url1 = 'https://api.bilibili.com/x/space/acc/info?mid=' + str(mid) + '&jsonp=jsonp'
        # url2 = 'https://api.bilibili.com/x/relation/stat?vmid=' + str(mid) + '&jsonp=jsonp'
        time.sleep(time_ )
        url_data = r.get(url=url,headers=headers)
        if url_data.json()['code'] == 22115:
            break

        # break/
        try:
            url_data = url_data.json()
            data = url_data['data']['list']
            for j in data:
                path = add_dir(str(j['mid']))
                if path != 0:
                    print(path)
                    print(j['face'])
                    img = r.get(url=j['face'], timeout=10)
                    print(img)
                    with open(path + '/' + str(j['mid']) + '.jpg', 'wb') as f:
                        f.write(img.content)
                        f.close()

                    with open(path+'/data.json', 'w', encoding='utf-8') as f:
                        json.dump(dict(j), f, ensure_ascii=False, indent=4)
                    now = datetime.now()
                    formatted_time = now.strftime("%Y-%m-%d %H:%M")
                    summary = {
                        '头像更新时间':formatted_time,
                        '数据更新时间':formatted_time,
                        '是否命中':False,
                        '命中规则':{
                            '头像':False,
                            '简介':False,
                            '视频':False,
                        },
                        '遍历':False
                    }
                    with open(path+'/summary.json', 'w', encoding='utf-8') as f:
                        json.dump(dict(summary), f, ensure_ascii=False, indent=4)

        except:
            traceback.print_exc()
        break
def dow_followings(mid):

    for i in range(1,6):
        url = 'https://api.bilibili.com/x/relation/followings?order=desc&order_type=&vmid='+mid+'&pn='+str(i)+'&ps=24&gaia_source=main_web&web_location=333.1387'
        print(url)
        # url1 = 'https://api.bilibili.com/x/space/acc/info?mid=' + str(mid) + '&jsonp=jsonp'
        # url2 = 'https://api.bilibili.com/x/relation/stat?vmid=' + str(mid) + '&jsonp=jsonp'
        time.sleep(time_ )
        url_data = r.get(url=url,headers=headers)
        # print(url_data.text)
        if url_data.json()['code'] == 22115:
            break
        try:
            url_data = url_data.json()
            data = url_data['data']['list']
            for j in data:
                path = add_dir(str(j['mid']))
                if path != 0:
                    print(path)

                    img = r.get(url=j['face'], timeout=10)
                    print(img)
                    with open(path+'/'+str(j['mid'])+'.jpg', 'wb') as f:
                        f.write(img.content)
                        f.close()

                    with open(path+'/data.json', 'w', encoding='utf-8') as f:
                        json.dump(dict(j), f, ensure_ascii=False, indent=4)
                    now = datetime.now()
                    formatted_time = now.strftime("%Y-%m-%d %H:%M")
                    summary = {
                        '头像更新时间': formatted_time,
                        '数据更新时间': formatted_time,
                        '是否命中': False,
                        '命中规则': {
                            '头像': False,
                            '简介': False,
                            '视频': False,
                        },
                        '遍历': False
                    }
                    with open(path + '/summary.json', 'w', encoding='utf-8') as f:
                        json.dump(dict(summary), f, ensure_ascii=False, indent=4)
        except:
            traceback.print_exc()
            with open('err_url.txt','a+',encoding='utf-8')as f:
                f.write(url)
                f.write('\n')


def get_all_folders(directory):
    # 获取目录中的所有文件和文件夹
    items = os.listdir(directory)
    # 筛选出文件夹
    folders = [item for item in items if os.path.isdir(os.path.join(directory, item))]
    return folders

def aa():
    folders = get_all_folders('data')
    for dir in folders:
        with open('data/' + dir + '/summary.json', 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        json_data['遍历'] = False
        with open('data/' + dir + '/summary.json', 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=4)
        print(dir, '完成')

def main():
    folders = get_all_folders('data')
    print(folders)
    for dir in folders:
        try:
            with open( 'data/' + dir + '/summary.json', 'r', encoding='utf-8') as file:
                json_data = json.load(file)
            if json_data['是否命中'] == True and json_data['遍历']==False:
                print(json_data['是否命中'])
                print(dir)
                dow_fans(dir)#查关注
                dow_followings(dir)#查粉丝
                json_data['遍历'] = True
                with open('data/' + dir + '/summary.json', 'w', encoding='utf-8') as f:
                    json.dump(json_data, f, ensure_ascii=False, indent=4)
                print(dir ,'完成')
        except:
            traceback.print_exc()
# aa()
# mid = '10122220'
# dow_fans(mid)#查关注
# dow_followings(mid)#查粉丝
# dow()
main()
