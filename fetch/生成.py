import os
import time
import json
from datetime import datetime
import requests as r
import xlsxwriter
from PIL import Image
import traceback
time_ = 0.5

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


def get_all_folders(directory):
    # 获取目录中的所有文件和文件夹
    items = os.listdir(directory)
    # 筛选出文件夹
    folders = [item for item in items if os.path.isdir(os.path.join(directory, item))]
    return folders



def dow(mid):
    directory_path = os.getcwd() + '/data'
    now = datetime.now()
    formatted_time = now.strftime("%Y-%m-%d %H:%M")
    url = 'https://api.bilibili.com/x/relation/stat?vmid='+mid+'&web_location=333.1387'
    print(url)
    time.sleep(time_)
    url_data = r.get(url=url,headers=headers)
    data = url_data.json()
    print(url_data.json())
    following = data['data']['following']
    follower = data['data']['follower']
    fans = {
        '数据更新时间': formatted_time,
        '粉丝': follower,
        '关注': following
    }
    with open(directory_path + '/' + mid + '/fans.json', 'w', encoding='utf-8') as f:
        json.dump(dict(fans), f, ensure_ascii=False, indent=4)

def addxlsx():
    directory_path = os.getcwd() + '/data'
    folders = get_all_folders(directory_path)
    datass = []
    for i in folders:
        print(i)
        try:
            datas = []
            with open(directory_path + '/' + i + '/data.json', 'r', encoding='utf-8') as file:
                json_data1 = json.load(file)
            try:
                with open(directory_path + '/' + i + '/fans.json', 'r', encoding='utf-8') as file:
                    json_data2 = json.load(file)
            except:
                json_data2 = {}
            with open(directory_path + '/' + i + '/summary.json', 'r', encoding='utf-8') as file:
                json_data3 = json.load(file)
            if json_data3['是否命中'] == True:
                datas.append(i)
                datas.append(json_data1['uname'])
                datas.append(json_data1['sign'])
                if json_data2 == {}:
                    datas.append('数据未知')
                    datas.append('数据未知')
                else:
                    datas.append(json_data2['粉丝'])
                    datas.append(json_data2['关注'])
                txt = ''
                if json_data3['命中规则']['头像']:
                    txt += '头像 '
                if json_data3['命中规则']['简介']:
                    txt += '简介 '
                if json_data3['命中规则']['视频']:
                    txt += '视频 '
                datas.append(txt)
                datass.append(datas)
        except:
            traceback.print_exc()
            print('文件损坏')

    # print(datass)
    data_long = len(datass)
    print('排序中')
    while True:
        b = True
        for i in range(data_long-1):
            if datass[i][3] == '数据未知':
                datass[i][3] = -1
            if datass[i+1][3] == '数据未知':
                datass[i + 1][3] = -1

            if int(datass[i][3]) < int(datass[i+1][3]):
                b = False
                tamp = datass[i]
                datass[i] = datass[i + 1]
                datass[i+1] = tamp
            if datass[i][3] == -1:
                datass[i][3] = '数据未知'

            if datass[i+1][3] == -1:
                datass[i + 1][3] = '数据未知'
        if b:
            break
    for i in datass:
        print(i)

    # 创建新工作簿
    workbook = xlsxwriter.Workbook('统计.xlsx')
    worksheet = workbook.add_worksheet()
    worksheet.write('A1', '头像')
    worksheet.write('B1', 'uid')
    worksheet.write('C1', '昵称')
    worksheet.write('D1', '简介')
    worksheet.write('E1', '粉丝')
    worksheet.write('F1', '关注')
    worksheet.write('G1', '命中规则')
    # worksheet.column_dimensions['A'].width = 45
    # worksheet.set_column(0, 0, 10)  # A列宽度45字符
    # worksheet.set_column(3, 3, 80)  # D列宽度200字符

    # 写入数据
    num = 2
    for i in datass:
        print(i)
        try:
            worksheet.set_row(num, 45)

            try:
                img = Image.open('data\\'+i[0]+'\\'+i[0]+'.jpg')
                img_resized = img.resize((70,70), Image.Resampling.LANCZOS)
                img_resized.save('data\\' + i[0] + '\\' + i[0] + '70x70.jpg')
                worksheet.insert_image(
                    'A' + str(num), 'data\\' + i[0] + '\\' + i[0] + '50x50.jpg', {'x_scale': 1, 'y_scale': 1})
            except Exception as e:
                traceback.print_exc()
                print("图片转换失败:", e)
                # worksheet.insert_image(
                #     'A' + str(num), 'data\\' + i[0] + '\\' + i[0] + '.jpg', {'x_scale': 0.3, 'y_scale': 0.3})
            worksheet.write('B'+str(num), i[0])
            worksheet.write('C'+str(num), i[1])
            worksheet.write('D'+str(num), i[2])
            worksheet.write('E'+str(num), i[3])
            worksheet.write('F'+str(num), i[4])
            worksheet.write('G' + str(num), i[5])
            num += 1
        except:
            print('插入数据错误')

    # 保存文件
    workbook.close()
def main():
    directory_path = os.getcwd() + '/data'
    folders = get_all_folders(directory_path)
    now = datetime.now()
    formatted_time = now.strftime("%Y-%m-%d %H:%M")
    # folders = ['144604']
    #
    # folders = ['66627']
    for files in folders:
        print(files)
        try:
            with open(directory_path + '/' + files + '/summary.json', 'r', encoding='utf-8') as file:
                json_data = json.load(file)
            if json_data['是否命中'] == True:
                try:
                    with open(directory_path + '/' + files + '/fans.json', 'r', encoding='utf-8') as file:
                        json_data = json.load(file)
                    timss = json_data['数据更新时间']
                except:
                    # traceback.print_exc()
                    print(files)
                    # break

                    dow(files)
                    # break

        except:
            traceback.print_exc()
            print('')

#
# while True:
#     time.sleep(60)
#     main()

addxlsx()


 # 创建新工作簿
def t():
    workbook = xlsxwriter.Workbook('统计1.xlsx')
    worksheet = workbook.add_worksheet()
    worksheet.write('A1', '头像')
    worksheet.write('B1', 'uid')
    worksheet.write('C1', '昵称')
    worksheet.write('D1', '简介')
    worksheet.write('E1', '粉丝')
    worksheet.write('F1', '关注')
    worksheet.write('G1', '命中规则')
    # worksheet.column_dimensions['A'].width = 45
    worksheet.set_column(0, 0, 10)  # A列宽度45字符
    worksheet.set_column(3, 3, 80)  # D列宽度200字符
    workbook.close()
# t()
#     break