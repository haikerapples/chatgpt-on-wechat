import os
import datetime
import json
import re
import time
from channel.chat_message import ChatMessage
from bridge.context import *
from channel.wechatnt.ntchat_WechatImageDecoder import WechatImageDecoder
from common.log import logger
import ntchat

#NT工具类
class NTTool(object):
    
    #init方法
    def __init__(self, wechatnt = None):
        super().__init__()
        
        #wechatnt
        self.wechatnt = wechatnt
        
    # 写文件
    def writeFile(self, fileName, directoryName, content):
        #创建文件夹
        directory = os.path.join(os.getcwd(), directoryName)
        if not os.path.exists(directory):
            os.makedirs(directory)
            
        with open(os.path.join(directory, fileName), 'w', encoding='utf-8') as f:
            json.dump(content, f, ensure_ascii=False, indent=4)
      
            
    # 读文件
    def readFile(self, fileName, directoryName):
        #文件路径
        filePath = os.path.join(os.getcwd(), directoryName, fileName)
        if not os.path.exists(filePath):
            return None
            
        # 从文件读取数据，并构建以 wxid 为键的字典
        with open(filePath, 'r', encoding='utf-8') as f:
            result = json.load(f)
            return result
        
        
    # 构造消息体对象
    def dealMessage(self, message):
        #登录信息
        login_info = self.wechatnt.get_login_info()
        nickname = login_info['nickname']
        user_id = login_info['wxid']
        
        #type类型
        type = message["type"]
        
        #是否引用消息
        msg = ""
        
        if type == "11061":
            #引用消息
            raw_msg = message["data"]["raw_msg"]
            
            #title == 当前说的话
            start_tag = "<title>"
            end_tag = "</title>"
            start_index = raw_msg.find(start_tag) + len(start_tag)
            end_index = raw_msg.find(end_tag)
            current_msg = raw_msg[start_index:end_index]
            
            #content == 之前说的话
            start_tag1 = "<content>"
            end_tag1 = "</content>"
            start_index1 = raw_msg.find(start_tag1) + len(start_tag1)
            end_index1 = raw_msg.find(end_tag1)
            orgin_msg = raw_msg[start_index1:end_index1]
            msg = orgin_msg + current_msg
        else:
            #消息内容
            msg = message["data"]["msg"]
        
        #群ID：209xxxxx@chatroom
        room_wxid = message["data"]["room_wxid"]
        #@我的用户ID列表：['wxid_pxxxxx']
        at_user_list = message["data"].get('at_user_list', [])
        #发消息用户ID：xxxxx
        from_wxid = message["data"]["from_wxid"]
        #接受消息用户ID：wxid_pxxxxx
        to_wxid = message["data"]["to_wxid"]
        #消息ID：4952821xxxxx
        msgid = message["data"]["msgid"]
        #时间戳：1691075115
        timestamp = message["data"]["timestamp"]
        #消息类型：1
        wx_type = message["data"]["wx_type"]
        
        #读取文件 - 群聊
        cacheDic = self.readFile("rooms.json", "tmp")
        rooms = {room['wxid']: room['nickname'] for room in cacheDic}
        
        #读取文件 - 好友
        cacheDic1 = self.readFile("contacts.json", "tmp")
        contacts = {contact['wxid']: contact['nickname'] for contact in cacheDic1}
        
        #读取文件 - 好友 + 房间
        room_members = self.readFile("room_members.json", "tmp")
        
        #构造context
        content_dict = {}
        content_dict["msg_id"] = msgid
        content_dict["create_time"] = timestamp
        #copy @机器人的消息，不会被识别为@，这里做兼容(例如： @robot 你好，此时如果是copy字符串时，无法被识别为@的消息)
        match_isCopyMsgToRobot = msg.startswith(f"@{nickname} ")
        if match_isCopyMsgToRobot:
           #去除@信息
           tempContent = msg.replace(f"@{nickname} ", "")
           msg = tempContent
        content_dict["content"] = msg
        content_dict["from_user_id"] = from_wxid
        content_dict["from_user_nickname"] = contacts.get(from_wxid)
        content_dict["to_user_id"] = to_wxid
        #当前机器人
        login_info = self.wechatnt.get_login_info()
        nickname = login_info['nickname']
        content_dict["to_user_nickname"] = nickname
        content_dict["other_user_id"] = from_wxid
        content_dict["other_user_nickname"] = contacts.get(from_wxid)
        isGroup = room_wxid is not None and room_wxid != ""
        content_dict["isgroup"] = isGroup
        #添加必要key
        content_dict["receiver"] = from_wxid
        content_dict["session_id"] = from_wxid
        if isGroup:
            data = message["data"]
            content_dict["other_user_nickname"] = rooms.get(data.get('room_wxid'))
            content_dict["other_user_id"] = data.get('room_wxid')
            is_at = (user_id in at_user_list) or match_isCopyMsgToRobot
            content_dict["is_at"] = is_at
            content_dict["actual_user_nickname"] = self.get_display_name_or_nickname(room_members, data.get('room_wxid'),from_wxid)
            #添加必要key
            content_dict["receiver"] = to_wxid
            content_dict["session_id"] = to_wxid
                        
        #获取其他字段
        tempDic = self.dealDictWithType(type, message)
        content_dict.update(tempDic)
        
        #msg对象
        msgObj : ChatMessage = ChatMessage(content_dict)
        #信息映射
        for key, value in content_dict.items():
            if hasattr(msgObj, key):
                setattr(msgObj, key, value)
        #处理message的is_group
        msgObj.is_group = isGroup
        content_dict["msg"] = msgObj
        
        #构造context
        context = Context(ContextType.TEXT, msg, content_dict)
        
        return context
        
    #获取群聊中的昵称
    def get_display_name_or_nickname(self, room_members, group_wxid, wxid):
        if group_wxid in room_members:
            for member in room_members[group_wxid]['member_list']:
                if member['wxid'] == wxid:
                    return member['display_name'] if member['display_name'] else member['nickname']
        return None  # 如果没有找到对应的group_wxid或wxid，则返回None
    
    #获取昵称
    def get_nickname(self, contacts, wxid):
        for contact in contacts:
            if contact['wxid'] == wxid:
                return contact['nickname']
        return None  # 如果没有找到对应的wxid，则返回None
   
    #根据消息类型 - 处理dict
    def dealDictWithType(self, type, message):
        data = message["data"]
        content_dict = {}
        tempType = None
        
        # 文本消息类型
        if type == 11046:  
            tempType = ContextType.TEXT
        
        #图片 - 需要缓存文件的消息类型
        elif type == 11047:  
            image_path = data.get('image').replace('\\', '/')
            #可读
            if self.ensure_file_ready(image_path):
                tempType = ContextType.IMAGE
                #图片解析器
                decoder = WechatImageDecoder(image_path)
                content_dict["content"] = decoder.decode()
            else:
                logger.error(f"图片文件不可读！Image file {image_path} is not ready.")
                
        #语音
        elif type == 11048: 
            tempType = ContextType.VOICE
            content_dict["content"] = data.get('mp3_file')
        
        #加群
        elif type == 11098:
            tempType = ContextType.JOIN_GROUP
            actual_user_nickname = data['member_list'][0]['nickname']
            content_dict["content"] = f"{actual_user_nickname}加入了群聊！"
            content_dict["actual_user_nickname"] = actual_user_nickname
            
            #读取文件
            cacheDic = self.readFile("room_members.json", "tmp")
            rooms = {room['wxid']: room['nickname'] for room in cacheDic}
            
            #写入文件
            result = {}
            for room_wxid in rooms.keys():
                room_members = self.wechatnt.get_room_members(room_wxid)
                result[room_wxid] = room_members
            self.writeFile("room_members.json", "tmp", result)
        
        #拍一拍    
        elif type == 11058 and "拍了拍" in data.get('raw_msg'):
            tempType = ContextType.PATPAT
            content_dict["content"] = data.get('raw_msg')
            
        else:
            logger.error(f"暂不支持的消息类型：{type}")
        
        #类型
        if tempType is not None:
            content_dict["ctype"] = tempType
            
        return content_dict