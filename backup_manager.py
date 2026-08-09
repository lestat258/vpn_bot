#!/usr/bin/env python3
"""
Модуль управления бэкапами — ПОЛНАЯ ВЕРСИЯ
"""
import os
import json
import sqlite3
import shutil
import tarfile
import glob
import subprocess
from datetime import datetime
import logging
import time

# Установка московского времени для Python
os.environ['TZ'] = 'Europe/Moscow'
try:
    time.tzset()
except AttributeError:
    pass

DB_PATH = "/opt/vpn-bot/data.db"
BACKUP_DIR = "/opt/vpn-bot/backups"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class BackupManager:
    def __init__(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)

    def create_full_backup(self):
        """Создаёт бэкап (БД + настройки)"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_folder = os.path.join(BACKUP_DIR, f"backup_{timestamp}")
        os.makedirs(backup_folder, exist_ok=True)

        logger.info(f"📦 Создание бэкапа: {backup_folder}")

        try:
            if os.path.exists(DB_PATH):
                shutil.copy2(DB_PATH, os.path.join(backup_folder, "data.db"))

            try:
                conn = sqlite3.connect(DB_PATH)
                with open(os.path.join(backup_folder, "data-dump.sql"), 'w') as f:
                    for line in conn.iterdump():
                        f.write(f'{line}\n')
                conn.close()
            except Exception as e:
                logger.warning(f"⚠️ SQL дамп не создан: {e}")

            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT key, value FROM settings")
                settings = dict(c.fetchall())
                conn.close()
                with open(os.path.join(backup_folder, "settings.json"), "w") as f:
                    json.dump(settings, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"⚠️ Настройки не сохранены: {e}")

            with open(os.path.join(backup_folder, "system-info.txt"), "w") as f:
                f.write(f"Дата: {datetime.now().isoformat()}\n")
                f.write(f"Сервер: {os.uname().nodename}\n")
                f.write(f"Система: {os.uname().sysname} {os.uname().release}\n")

            archive_name = f"backup_{timestamp}.tar.gz"
            archive_path = os.path.join(BACKUP_DIR, archive_name)
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(backup_folder, arcname=os.path.basename(backup_folder))

            shutil.rmtree(backup_folder)
            self._cleanup_old_backups()

            logger.info(f"✅ Бэкап создан: {archive_path}")
            return {
                "success": True,
                "timestamp": timestamp,
                "archive": archive_path,
                "size": os.path.getsize(archive_path)
            }

        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            if os.path.exists(backup_folder):
                shutil.rmtree(backup_folder)
            return {"success": False, "error": str(e)}

    def create_full_project_backup(self):
        """Создаёт ПОЛНЫЙ бэкап всего проекта (как ручной)"""
        import glob
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        project_dir = "/opt/vpn-bot"
        backup_name = f"project_backup_{timestamp}.tar.gz"
        backup_path = os.path.join(BACKUP_DIR, backup_name)

        logger.info(f"📦 Создание ПОЛНОГО бэкапа проекта: {backup_path}")

        try:
            exclude_dirs = [
                "__pycache__",
                "logs",
                "backups",
                ".git",
                ".pytest_cache",
                "htmlcov",
                ".coverage"
            ]
            exclude_ext = [".pyc", ".pyo", ".pid", ".lock", ".db-journal", ".db-shm", ".db-wal"]

            with tarfile.open(backup_path, "w:gz") as tar:
                # 1. Все файлы проекта (кроме исключённых)
                for root, dirs, files in os.walk(project_dir):
                    dirs[:] = [d for d in dirs if d not in exclude_dirs]

                    for file in files:
                        if any(file.endswith(ext) for ext in exclude_ext):
                            continue
                        if file.startswith("backup_") or file.startswith("project_backup_"):
                            continue
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, project_dir)
                        tar.add(file_path, arcname=arcname)

                # 2. Systemd сервисы
                systemd_dir = "/etc/systemd/system"
                if os.path.exists(systemd_dir):
                    for service in glob.glob(f"{systemd_dir}/vpn-*.service"):
                        tar.add(service, arcname=f"systemd/{os.path.basename(service)}")
                        logger.info(f"✅ Добавлен systemd: {os.path.basename(service)}")

                # 3. Crontab
                crontab_file = "/tmp/crontab_backup.txt"
                try:
                    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
                    if result.returncode == 0 and result.stdout.strip():
                        with open(crontab_file, 'w') as f:
                            f.write(result.stdout)
                        tar.add(crontab_file, arcname="crontab.txt")
                        logger.info("✅ Добавлен crontab")
                except Exception as e:
                    logger.warning(f"⚠️ Crontab не сохранён: {e}")
                finally:
                    if os.path.exists(crontab_file):
                        os.remove(crontab_file)

                # 4. .env файл (если есть)
                env_file = "/opt/vpn-bot/.env"
                if os.path.exists(env_file):
                    tar.add(env_file, arcname=".env")
                    logger.info("✅ Добавлен .env")

                # 5. SQL дамп
                dump_file = "/tmp/data-dump.sql"
                try:
                    result = subprocess.run(
                        ["sqlite3", "/opt/vpn-bot/data.db", ".dump"],
                        capture_output=True,
                        text=True
                    )
                    if result.returncode == 0 and result.stdout:
                        with open(dump_file, 'w') as f:
                            f.write(result.stdout)
                        tar.add(dump_file, arcname="data-dump.sql")
                        logger.info("✅ Добавлен SQL дамп")
                except Exception as e:
                    logger.warning(f"⚠️ SQL дамп не создан: {e}")
                finally:
                    if os.path.exists(dump_file):
                        os.remove(dump_file)

                # 6. Информация о системе
                info_file = "/tmp/system-info.txt"
                with open(info_file, 'w') as f:
                    f.write(f"Дата: {datetime.now().isoformat()}\n")
                    f.write(f"Сервер: {os.uname().nodename}\n")
                    f.write(f"Система: {os.uname().sysname} {os.uname().release}\n")
                tar.add(info_file, arcname="system-info.txt")
                os.remove(info_file)

            size = os.path.getsize(backup_path)
            logger.info(f"✅ Полный бэкап проекта создан: {backup_path} ({size / 1024 / 1024:.2f} MB)")

            return {
                "success": True,
                "timestamp": timestamp,
                "archive": backup_path,
                "size": size,
                "name": backup_name
            }

        except Exception as e:
            logger.error(f"❌ Ошибка создания бэкапа проекта: {e}")
            if os.path.exists(backup_path):
                os.remove(backup_path)
            return {"success": False, "error": str(e)}

    def _cleanup_old_backups(self, keep=10):
        files = glob.glob(os.path.join(BACKUP_DIR, "backup_*.tar.gz"))
        files.sort(key=os.path.getctime, reverse=True)
        for file in files[keep:]:
            os.remove(file)

    def get_backup_list(self):
        backups = []
        for file in glob.glob(os.path.join(BACKUP_DIR, "*.tar.gz")):
            try:
                stat = os.stat(file)
                name = os.path.basename(file)
                timestamp = name.replace("backup_", "").replace("project_backup_", "").replace(".tar.gz", "")
                backups.append({
                    "timestamp": timestamp,
                    "file": name,
                    "size": stat.st_size,
                    "datetime": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
                    "type": "project" if name.startswith("project_backup_") else "full"
                })
            except:
                pass
        return sorted(backups, key=lambda x: x["timestamp"], reverse=True)

backup_manager = BackupManager()
