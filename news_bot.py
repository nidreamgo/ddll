import os
import feedparser
import smtplib
import requests
import json
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import List, Dict, Set
import sys

# ========== 配置区域 ==========
MAX_ARTICLES_PER_SOURCE = 25  # 每个源最多取25条（已修改）
SUMMARY_LENGTH = 150  # 摘要长度（字符数）

# 读取环境变量
MAIL_USER = os.getenv('MAIL_USER')
MAIL_PASS = os.getenv('MAIL_PASS')
MAIL_TO = os.getenv('MAIL_TO')
FEISHU_WEBHOOK = os.getenv('FEISHU_WEBHOOK')

# ========== 读取RSS源（必须从文件读取） ==========
def load_rss_sources():
    """必须从rss_sources.txt读取RSS源，文件不存在时报错退出"""
    sources = []
    file_path = 'rss_sources.txt'
    
    if not os.path.exists(file_path):
        print(f"错误：找不到RSS源文件 {file_path}")
        print("请创建该文件，每行格式：源名称|RSS地址")
        print("例如：36氪|https://36kr.com/feed")
        sys.exit(1)  # 退出程序
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split('|')
                if len(parts) != 2:
                    print(f"警告：第{line_num}行格式错误，已跳过：{line}")
                    continue
                
                sources.append({'name': parts[0].strip(), 'url': parts[1].strip()})
        
        if not sources:
            print(f"错误：{file_path} 中没有有效的RSS源配置")
            sys.exit(1)
            
        print(f"成功加载 {len(sources)} 个RSS源")
        return sources
        
    except Exception as e:
        print(f"读取RSS源文件失败: {e}")
        sys.exit(1)

# ========== 读取关键词（必须从文件读取） ==========
def load_keywords() -> Set[str]:
    """必须从keywords.txt读取关键词，文件不存在时报错退出"""
    keywords = set()
    file_path = 'keywords.txt'
    
    if not os.path.exists(file_path):
        print(f"错误：找不到关键词文件 {file_path}")
        print("请创建该文件，每行一个关键词")
        print("例如：融资\n商机\nIPO")
        sys.exit(1)  # 退出程序
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # 关键词统一转为小写存储（便于匹配）
                keywords.add(line.lower())
        
        if not keywords:
            print(f"错误：{file_path} 中没有有效的关键词")
            sys.exit(1)
            
        print(f"成功加载 {len(keywords)} 个关键词")
        return keywords
        
    except Exception as e:
        print(f"读取关键词文件失败: {e}")
        sys.exit(1)

# ========== 抓取RSS ==========
def fetch_rss() -> List[Dict]:
    """抓取所有RSS源，获取最新文章"""
    sources = load_rss_sources()
    keywords = load_keywords()
    all_articles = []
    
    print(f"开始抓取 {len(sources)} 个RSS源，每个源最多取 {MAX_ARTICLES_PER_SOURCE} 条...")
    
    for source in sources:
        try:
            print(f"正在抓取: {source['name']} ({source['url']})")
            feed = feedparser.parse(source['url'])
            
            # 检查feed是否解析成功
            if hasattr(feed, 'status') and feed.status != 200:
                print(f"警告：{source['name']} 返回状态码 {feed.status}")
                continue
                
            if not feed.entries:
                print(f"警告：{source['name']} 没有获取到任何条目")
                continue
            
            count = 0
            source_articles = []
            
            for entry in feed.entries:
                if count >= MAX_ARTICLES_PER_SOURCE:
                    break
                
                # 提取文章信息
                title = entry.get('title', '无标题')
                link = entry.get('link', '#')
                published = entry.get('published', entry.get('updated', ''))
                summary = entry.get('summary', entry.get('description', ''))
                
                # 去除HTML标签
                summary = re.sub(r'<[^>]+>', '', summary)
                summary = summary[:500]  # 限制长度
                
                # 检查是否包含关键词
                content_text = (title + ' ' + summary).lower()
                matched_keywords = [kw for kw in keywords if kw in content_text]
                
                if matched_keywords:
                    article = {
                        'title': title,
                        'link': link,
                        'published': published,
                        'summary': summary,
                        'source': source['name'],
                        'keywords': matched_keywords[:3]  # 只取前3个匹配的关键词
                    }
                    source_articles.append(article)
                    count += 1
            
            all_articles.extend(source_articles)
            print(f"  └─ 从 {source['name']} 获取到 {len(source_articles)} 篇匹配文章")
                    
        except Exception as e:
            print(f"抓取 {source['name']} 失败: {e}")
    
    print(f"抓取完成，共 {len(all_articles)} 篇文章匹配关键词")
    return all_articles

# ========== 生成推送内容 ==========
def generate_message(articles: List[Dict]) -> str:
    """生成推送的图文消息"""
    if not articles:
        return "今日暂无匹配关键词的资讯"
    
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 按来源分组
    by_source = {}
    for article in articles:
        source = article['source']
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(article)
    
    # 构建消息
    msg = f"📰 **商业资讯摘要** ({today})\n\n"
    msg += f"共为您筛选出 **{len(articles)}** 篇相关资讯\n"
    msg += "---\n\n"
    
    for source, items in by_source.items():
        msg += f"### 📌 {source}\n\n"
        for i, item in enumerate(items[:5], 1):  # 每个源最多显示5条
            # 标题 + 关键词标签
            keywords_str = ' '.join([f"`{kw}`" for kw in item['keywords']])
            msg += f"**{i}. [{item['title']}]({item['link']})** {keywords_str}\n\n"
            
            # 摘要（先显示总结）
            summary = item['summary'][:SUMMARY_LENGTH]
            if len(item['summary']) > SUMMARY_LENGTH:
                summary += "..."
            msg += f"> {summary}\n\n"
            
            # 原文链接（全文）
            msg += f"[📖 阅读全文]({item['link']})\n\n"
        
        msg += "---\n\n"
    
    msg += f"\n🕒 推送时间：{today}\n"
    msg += "---\n"
    msg += "如需调整关键词，请修改 keywords.txt 文件"
    
    return msg

# ========== 推送邮件 ==========
def send_email(content: str):
    """发送邮件到多个收件人"""
    if not all([MAIL_USER, MAIL_PASS, MAIL_TO]):
        print("邮件配置不完整，跳过邮件推送")
        return
    
    try:
        # 多个邮箱处理
        to_list = [email.strip() for email in MAIL_TO.split(',')]
        
        # 创建邮件
        message = MIMEMultipart()
        message['From'] = MAIL_USER
        message['To'] = ', '.join(to_list)
        message['Subject'] = f"商业资讯摘要 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        # 将Markdown转为纯文本（简单处理）
        plain_text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)
        plain_text = re.sub(r'[#*`>]', '', plain_text)
        
        message.attach(MIMEText(plain_text, 'plain', 'utf-8'))
        
        # 连接SMTP服务器
        server = smtplib.SMTP_SSL('smtp.qq.com', 465)
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(message)
        server.quit()
        
        print(f"邮件推送成功，收件人: {to_list}")
    except Exception as e:
        print(f"邮件推送失败: {e}")

# ========== 推送飞书 ==========
def send_feishu(content: str):
    """推送消息到飞书"""
    if not FEISHU_WEBHOOK:
        print("飞书Webhook未配置，跳过飞书推送")
        return
    
    try:
        # 飞书消息格式（支持富文本）
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "content": f"📰 商业资讯摘要 {datetime.now().strftime('%m-%d')}",
                        "tag": "plain_text"
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content.replace('**', '*')  # 飞书markdown用*表示加粗
                    }
                ]
            }
        }
        
        response = requests.post(
            FEISHU_WEBHOOK,
            json=payload,
            headers={'Content-Type': 'application/json'}
        )
        
        if response.status_code == 200:
            print("飞书推送成功")
        else:
            print(f"飞书推送失败: {response.text}")
            
    except Exception as e:
        print(f"飞书推送异常: {e}")

# ========== 主函数 ==========
def main():
    print("="*50)
    print(f"开始执行推送任务 - {datetime.now()}")
    print("="*50)
    
    # 1. 抓取文章（会自动检查文件是否存在）
    articles = fetch_rss()
    
    # 2. 生成消息
    message = generate_message(articles)
    
    # 3. 打印预览
    print("\n推送内容预览:")
    print(message[:500] + "...\n")
    
    # 4. 推送邮件
    if MAIL_USER and MAIL_PASS and MAIL_TO:
        send_email(message)
    else:
        print("邮件配置不完整，跳过")
    
    # 5. 推送飞书
    if FEISHU_WEBHOOK:
        send_feishu(message)
    else:
        print("飞书配置不完整，跳过")
    
    print("\n" + "="*50)
    print("任务执行完成")
    print("="*50)

if __name__ == "__main__":
    main()
