import os
import sys
import re
import hashlib
import requests
from bs4 import BeautifulSoup
import feedparser
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import pytz
from typing import List, Dict, Set, Tuple, Optional
import json
import logging

# ========== 配置 ==========
MAX_ARTICLES_PER_SOURCE = 25   # 每个源最多取25条（防止抓取过多）
BATCH_SIZE = 50                # 每批推送的最大条数
SUMMARY_MAX_LEN = 200          # 摘要最大长度（字符）

# 环境变量
MAIL_USER = os.getenv('MAIL_USER')
MAIL_PASS = os.getenv('MAIL_PASS')
MAIL_TO = os.getenv('MAIL_TO')
FEISHU_WEBHOOK = os.getenv('FEISHU_WEBHOOK')

# 时区
TZ = pytz.timezone('Asia/Shanghai')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== 辅助函数 ==========
def get_beijing_time() -> str:
    """获取当前北京时间字符串"""
    now = datetime.now(TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S")

def extract_article_text(html_content: str, url: str) -> str:
    """从HTML中提取正文（简单提取，可自行扩展）"""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除脚本和样式
        for script in soup(["script", "style"]):
            script.decompose()
        
        # 优先使用 article 标签
        article = soup.find('article')
        if article:
            text = article.get_text()
        else:
            # 尝试常见内容容器
            selectors = [
                'div.content', 
                'div.post-content', 
                'div.entry-content', 
                'main', 
                '.article-body',
                '.post-body',
                '.content-body'
            ]
            text = None
            for selector in selectors:
                container = soup.select_one(selector)
                if container:
                    text = container.get_text()
                    break
            if not text:
                text = soup.get_text()
        
        # 清理空白
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    except Exception as e:
        logger.error(f"提取正文失败 {url}: {e}")
        return ""

def summarize_content(content: str, url: str) -> str:
    """提取摘要：去除作者/编辑信息，保留前SUMMARY_MAX_LEN字符"""
    if not content:
        return "暂无摘要"
    
    # 去掉常见的作者/编辑行
    lines = content.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        # 跳过常见的元数据行
        if re.match(r'^(作者|编辑|文/|来源|原标题|责编|记者|本文来源|原创|投稿)\s*[:：]', line, re.I):
            continue
        # 跳过空行
        if line:
            cleaned_lines.append(line)
    
    cleaned = ' '.join(cleaned_lines)
    # 截取前SUMMARY_MAX_LEN字符
    if len(cleaned) > SUMMARY_MAX_LEN:
        cleaned = cleaned[:SUMMARY_MAX_LEN] + '...'
    
    return cleaned if cleaned else "暂无摘要"

def fetch_from_html(url: str, source_name: str) -> List[Dict]:
    """抓取普通网页，返回文章列表（单篇文章）"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding  # 自动检测编码
        html = resp.text
        
        # 提取正文
        text = extract_article_text(html, url)
        
        # 提取标题
        soup = BeautifulSoup(html, 'html.parser')
        title_tag = soup.find('title')
        title = title_tag.string.strip() if title_tag and title_tag.string else source_name
        title = title[:200]
        
        return [{
            'title': title,
            'link': url,
            'published': get_beijing_time(),
            'summary': text[:1000] if text else "",  # 保存原始文本用于关键词匹配
            'source': source_name,
            'keywords': []
        }]
    except requests.RequestException as e:
        logger.error(f"抓取普通网页失败 {url}: {e}")
        return []
    except Exception as e:
        logger.error(f"处理普通网页失败 {url}: {e}")
        return []

def fetch_rss(url: str, source_name: str, max_articles: int) -> List[Dict]:
    """抓取RSS源，返回文章列表"""
    articles = []
    try:
        feed = feedparser.parse(url)
        
        if feed.bozo:  # 检查解析错误
            logger.warning(f"RSS解析警告 {source_name}: {feed.bozo_exception}")
        
        if not feed.entries:
            logger.warning(f"{source_name} 没有文章条目")
            return []
        
        for entry in feed.entries[:max_articles]:
            title = entry.get('title', '无标题')
            link = entry.get('link', '#')
            
            # 处理发布时间
            published = entry.get('published', entry.get('updated', ''))
            if published:
                # 尝试解析时间
                try:
                    # 如果时间格式是时间戳，转换为字符串
                    if isinstance(published, (int, float)):
                        published = datetime.fromtimestamp(published, TZ).strftime("%Y-%m-%d %H:%M:%S")
                except:
                    pass
            
            # 获取摘要
            summary = entry.get('summary', entry.get('description', ''))
            if summary:
                summary = re.sub(r'<[^>]+>', '', summary)  # 去HTML标签
                summary = summary[:1000]  # 限制长度
            else:
                summary = ""
            
            articles.append({
                'title': title,
                'link': link,
                'published': published or get_beijing_time(),
                'summary': summary,
                'source': source_name,
                'keywords': []
            })
            
    except Exception as e:
        logger.error(f"抓取RSS失败 {source_name}: {e}")
    
    return articles

def load_rss_sources() -> List[Dict]:
    """从 rss_sources.txt 读取源，支持 rss 和 html 类型"""
    sources = []
    file_path = 'rss_sources.txt'
    
    if not os.path.exists(file_path):
        logger.error(f"找不到配置文件: {file_path}")
        sys.exit(1)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split('|')
                if len(parts) < 2:
                    logger.warning(f"第{line_num}行格式错误，跳过: {line}")
                    continue
                
                name = parts[0].strip()
                url = parts[1].strip()
                typ = parts[2].strip().lower() if len(parts) >= 3 else 'rss'
                
                sources.append({
                    'name': name, 
                    'url': url, 
                    'type': typ
                })
    except Exception as e:
        logger.error(f"读取配置文件失败: {e}")
        sys.exit(1)
    
    if not sources:
        logger.error("没有有效的RSS源配置")
        sys.exit(1)
    
    logger.info(f"加载了 {len(sources)} 个信息源")
    return sources

def load_keywords() -> Set[str]:
    """从 keywords.txt 读取关键词"""
    keywords = set()
    file_path = 'keywords.txt'
    
    if not os.path.exists(file_path):
        logger.error(f"找不到关键词文件: {file_path}")
        sys.exit(1)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith('#'):
                    keywords.add(line)
    except Exception as e:
        logger.error(f"读取关键词文件失败: {e}")
        sys.exit(1)
    
    if not keywords:
        logger.error("没有有效关键词")
        sys.exit(1)
    
    logger.info(f"加载了 {len(keywords)} 个关键词")
    return keywords

def load_pushed_urls() -> Set[str]:
    """加载已推送的URL哈希集合"""
    try:
        with open('pushed_urls.txt', 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        logger.info("创建新的推送记录文件")
        return set()
    except Exception as e:
        logger.error(f"读取推送记录失败: {e}")
        return set()

def save_pushed_urls(pushed_set: Set[str]):
    """保存已推送的URL哈希"""
    try:
        with open('pushed_urls.txt', 'w', encoding='utf-8') as f:
            for url_hash in pushed_set:
                f.write(url_hash + '\n')
        logger.info(f"已保存 {len(pushed_set)} 条推送记录")
    except Exception as e:
        logger.error(f"保存推送记录失败: {e}")

def fetch_articles() -> Tuple[List[Dict], Set[str]]:
    """抓取所有文章，并去重"""
    sources = load_rss_sources()
    keywords = load_keywords()
    pushed_urls = load_pushed_urls()
    new_pushed = set()
    all_articles = []
    
    logger.info(f"开始抓取 {len(sources)} 个源，每个源最多 {MAX_ARTICLES_PER_SOURCE} 条")
    
    for src in sources:
        logger.info(f"正在处理: {src['name']} ({src['url']})")
        
        # 抓取文章
        if src['type'] == 'html':
            articles = fetch_from_html(src['url'], src['name'])
        else:
            articles = fetch_rss(src['url'], src['name'], MAX_ARTICLES_PER_SOURCE)
        
        if not articles:
            logger.info(f"  {src['name']} 未抓取到文章")
            continue
        
        # 关键词过滤 + 去重
        matched_count = 0
        for art in articles:
            # 检查是否包含关键词
            content_text = (art['title'] + ' ' + art['summary']).lower()
            matched = [kw for kw in keywords if kw in content_text]
            
            if not matched:
                continue
            
            # 去重检查
            url_hash = hashlib.md5(art['link'].encode()).hexdigest()
            if url_hash in pushed_urls:
                logger.debug(f"  跳过已推送: {art['title']}")
                continue
            
            # 添加关键词和哈希
            art['keywords'] = matched[:3]
            art['url_hash'] = url_hash
            
            all_articles.append(art)
            new_pushed.add(url_hash)
            matched_count += 1
        
        logger.info(f"  {src['name']} 匹配 {matched_count} 篇新文章")
    
    # 更新已推送记录
    updated_pushed = pushed_urls.union(new_pushed)
    save_pushed_urls(updated_pushed)
    
    logger.info(f"总计抓取 {len(all_articles)} 篇新文章")
    return all_articles, new_pushed

def format_message(articles: List[Dict]) -> str:
    """按新格式生成消息"""
    if not articles:
        return "今日暂无匹配关键词的资讯"
    
    # 统计各源数量
    source_count = {}
    for art in articles:
        src = art['source']
        source_count[src] = source_count.get(src, 0) + 1
    
    # 统计所有关键词频率
    kw_freq = {}
    for art in articles:
        for kw in art['keywords']:
            kw_freq[kw] = kw_freq.get(kw, 0) + 1
    
    # 取出现最多的5个关键词
    top_kws = sorted(kw_freq.items(), key=lambda x: x[1], reverse=True)[:5]
    top_kw_str = '、'.join([f"{kw}({cnt})" for kw, cnt in top_kws]) if top_kws else '无'
    
    # 构建头部
    beijing_time = get_beijing_time()
    header = f"📰 **商业资讯摘要** ({beijing_time})\n\n"
    header += f"共计筛选出 **{len(articles)}** 条相关资讯\n"
    
    # 各源统计
    src_lines = []
    for src, cnt in source_count.items():
        src_lines.append(f"{src} {cnt}条")
    header += "，".join(src_lines) + "\n"
    header += f"主要关键词：{top_kw_str}\n\n"
    
    # 按来源分组
    by_source = {}
    for art in articles:
        by_source.setdefault(art['source'], []).append(art)
    
    body = ""
    for src, items in by_source.items():
        # 生成来源标题
        body += f"❤️❤️{src}：{len(items)}条\n"
        
        for idx, art in enumerate(items, 1):
            # 标题+链接+关键词
            kw_str = '、'.join(art['keywords'])
            title_line = f"{idx}. **{art['title']}**\n   链接：{art['link']}\n   关键词：{kw_str}\n"
            body += title_line
            
            # 正文摘要
            summary = summarize_content(art['summary'], art['link'])
            body += f"   摘要：{summary}\n\n"
        
        body += "---\n\n"
    
    footer = f"推送时间：{beijing_time}"
    return header + body + footer

def send_email(content: str):
    """发送邮件"""
    if not all([MAIL_USER, MAIL_PASS, MAIL_TO]):
        logger.warning("邮件配置不完整，跳过")
        return
    
    try:
        to_list = [email.strip() for email in MAIL_TO.split(',')]
        
        # 创建纯文本版本
        plain_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)  # 去除markdown加粗
        plain_text = re.sub(r'[#*`>]', '', plain_text)
        
        msg = MIMEText(plain_text, 'plain', 'utf-8')
        msg['From'] = MAIL_USER
        msg['To'] = ', '.join(to_list)
        msg['Subject'] = f"商业资讯摘要 {datetime.now(TZ).strftime('%Y-%m-%d')}"
        
        server = smtplib.SMTP_SSL('smtp.qq.com', 465)
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"邮件发送成功，收件人: {MAIL_TO}")
        
    except smtplib.SMTPAuthenticationError:
        logger.error("邮件认证失败，请检查邮箱账号和授权码")
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")

def send_feishu(content: str):
    """推送飞书"""
    if not FEISHU_WEBHOOK:
        logger.warning("飞书未配置，跳过")
        return
    
    try:
        # 飞书卡片消息
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "content": f"📰 商业资讯摘要 {datetime.now(TZ).strftime('%m-%d')}",
                        "tag": "plain_text"
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content.replace('**', '**')  # 飞书支持markdown加粗
                    }
                ]
            }
        }
        
        # 如果内容过长，飞书可能有限制，可以截断
        if len(content) > 30000:
            content = content[:30000] + "...\n(内容过长已截断)"
            payload["card"]["elements"][0]["content"] = content
        
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        
        if resp.status_code == 200:
            result = resp.json()
            if result.get('code') == 0:
                logger.info("飞书推送成功")
            else:
                logger.error(f"飞书推送失败: {result}")
        else:
            logger.error(f"飞书推送HTTP错误: {resp.status_code}")
            
    except requests.RequestException as e:
        logger.error(f"飞书推送网络异常: {e}")
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")

def main():
    """主函数"""
    logger.info("="*50)
    logger.info(f"开始执行推送任务 - {get_beijing_time()}")
    logger.info("="*50)
    
    try:
        # 抓取文章
        articles, new_pushed = fetch_articles()
        logger.info(f"本次新文章数量: {len(articles)}")
        
        if not articles:
            logger.info("没有新文章，结束任务")
            # 发送空报告（可选）
            # empty_msg = "今日无新资讯"
            # send_email(empty_msg)
            # send_feishu(empty_msg)
            return
        
        # 生成完整消息
        full_msg = format_message(articles)
        
        # 分批次推送
        if len(articles) > BATCH_SIZE:
            logger.info(f"文章超过{BATCH_SIZE}条，分批次推送")
            for i in range(0, len(articles), BATCH_SIZE):
                batch_articles = articles[i:i+BATCH_SIZE]
                batch_msg = format_message(batch_articles)
                send_email(batch_msg)
                send_feishu(batch_msg)
                logger.info(f"已推送第 {i//BATCH_SIZE + 1} 批，共 {len(batch_articles)} 条")
        else:
            send_email(full_msg)
            send_feishu(full_msg)
        
        logger.info("任务执行完成")
        
    except Exception as e:
        logger.error(f"任务执行失败: {e}", exc_info=True)
        sys.exit(1)
    
    logger.info("="*50)

if __name__ == "__main__":
    main()
