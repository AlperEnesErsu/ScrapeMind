"""Yeni proje için flask-core-base template'ini dışa aktar.

Bu script, mevcut repodaki ScrapeMind'a özgü modülleri silerek
temiz bir flask-core-base iskeleti bırakır.

Kullanım (yeni bir dizine kopyaladıktan sonra):
    python scripts/export_core_template.py --target D:\\projects\\yeni_proje

UYARI: Hedef dizin üzerinde çalışır. Kaynak repoyu bozmaz.
"""

import argparse
import shutil
import sys
from pathlib import Path

# Kaldırılacak ScrapeMind-özgü yollar (hedef dizine göre relatif)
SCRAPEMIND_PATHS = [
    "app/modules/scrape",
    "app/modules/academic",
    "app/tasks/scrape_tasks.py",
    "app/tasks/schedule.py",
    "app/core/templates/settings/_tab_interests.html",  # academic modülüne taşındı ama yedek kalmışsa
    "app/core/templates/settings/_tab_identifiers.html",
]

# Yeni projede güncellenmesi gerekenler (script elle yapar)
REPLACEMENTS = {
    "scrapemind": None,  # proje adıyla değiştirilecek
}


def main():
    parser = argparse.ArgumentParser(description="flask-core-base template export")
    parser.add_argument("--target", required=True, help="Hedef dizin")
    parser.add_argument("--name", default="myproject", help="Yeni proje adı (küçük harf)")
    parser.add_argument("--dry-run", action="store_true", help="Sadece göster, silme")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if not target.exists():
        print(f"Hata: {target} bulunamadı.")
        sys.exit(1)

    print(f"Hedef: {target}")
    print(f"Proje adı: {args.name}")
    print()

    # 1. ScrapeMind özgü dosyaları/klasörleri sil
    for rel_path in SCRAPEMIND_PATHS:
        full = target / rel_path
        if full.exists():
            action = "SİLİNECEK" if args.dry_run else "SİLİNDİ"
            if full.is_dir():
                if not args.dry_run:
                    shutil.rmtree(full)
                print(f"  [{action}] {rel_path}/")
            else:
                if not args.dry_run:
                    full.unlink()
                print(f"  [{action}] {rel_path}")
        else:
            print(f"  [ATLA]    {rel_path} (bulunamadı)")

    # 2. app/__init__.py'deki ScrapeMind blueprint kayıtlarını kaldır
    init_file = target / "app/__init__.py"
    if init_file.exists() and not args.dry_run:
        content = init_file.read_text(encoding="utf-8")
        lines_to_remove = [
            "from app.modules.scrape.routes import scrape_bp",
            "from app.modules.academic import academic_bp",
            'app.register_blueprint(scrape_bp, url_prefix="/papers")',
            'app.register_blueprint(academic_bp, url_prefix="/academic")',
        ]
        new_lines = [l for l in content.splitlines()
                     if not any(rm.strip() in l for rm in lines_to_remove)]
        init_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        print(f"\n  [GÜNCELLENDI] app/__init__.py — scrape/academic blueprint'leri kaldırıldı")

    # 3. seed.py'deki academic import'u kaldır
    seed_file = target / "scripts/seed.py"
    if seed_file.exists() and not args.dry_run:
        content = seed_file.read_text(encoding="utf-8")
        lines_to_remove = [
            "from app.modules.academic.models import IdentifierType",
            "identifier_seeds",
            "identifier_types",
        ]
        # Basit yaklaşım: kullanıcıya bildir
        print(f"\n  [MANUEL]  scripts/seed.py — academic seed bloğunu elle kaldır")

    print("\n✓ Tamamlandı." if not args.dry_run else "\n(Dry-run — hiçbir şey silinmedi)")
    print(f"\nSonraki adımlar:")
    print(f"  1. app/__init__.py'yi gözden geçir")
    print(f"  2. scripts/seed.py'deki academic bloğunu kaldır")
    print(f"  3. app/modules/_template/'ı kullanarak ilk modülünü oluştur")
    print(f"  4. flask db migrate -m 'initial' && flask db upgrade")
    print(f"  5. python scripts/seed.py")


if __name__ == "__main__":
    main()
