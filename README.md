# samat-3d

خط لوله‌ی ساخت خودکار مدل سه‌بعدی از عکس‌های پهباد برای اپ SAMAT، با OpenDroneMap روی GitHub Actions.

**این ریپو عمومی است. همه‌ی عکس‌ها و مدل‌ها فقط به‌صورت رمزشده (AES-256-GCM) اینجا قرار می‌گیرند.**

## جریان کار

1. اپ برای هر پروژه یک `job_id` تصادفی (۱۶ تا ۳۲ کاراکتر hex) و یک کلید ۲۵۶ بیتی تصادفی می‌سازد. کلید فقط در Supabase نگهداری می‌شود.
2. عکس‌ها در مرورگر رمز می‌شوند و به‌صورت asset‌های `NNNN.enc` در Release با تگ `in-<job_id>` آپلود می‌شوند (از طریق Edge Function، نه با توکن داخل اپ).
3. Edge Function ورک‌فلو `build-3d.yml` را با ورودی `job_id` اجرا می‌کند.
4. ورک‌فلو کلید را از Edge Function می‌گیرد، عکس‌ها را رمزگشایی می‌کند، ODM را اجرا می‌کند و `model.glb` را رمز کرده در Release با تگ `model-<job_id>` می‌گذارد.
5. عکس‌های خام (Release `in-...`) حذف می‌شوند و نتیجه به اپ اطلاع داده می‌شود.
6. اپ هر وقت لازم شد `model.glb.enc` را می‌گیرد، در مرورگر رمزگشایی می‌کند و نشان می‌دهد. در Supabase فقط یک ردیف کوچک (وضعیت و تگ) می‌ماند.

## Secrets لازم (Settings > Secrets and variables > Actions)

| نام | کاربرد |
|---|---|
| `KEY_URL` | آدرس Edge Function که کلید را می‌دهد |
| `KEY_TOKEN` | توکن احراز هویت آن Edge Function |
| `CALLBACK_URL` | آدرس Edge Function که نتیجه را می‌گیرد (اختیاری) |
| `CALLBACK_TOKEN` | توکن آن (اختیاری) |
| `TEST_KEY_B64` | فقط برای تست دستی، وقتی `KEY_URL` تنظیم نشده |

قرارداد `KEY_URL`: درخواست `POST {"job_id": "..."}` با هدر `Authorization: Bearer <KEY_TOKEN>` و پاسخ `{"key_b64": "..."}`. Edge Function فقط برای `job_id`هایی که واقعاً در Supabase ثبت شده‌اند کلید بدهد.

ورودی‌ها و لاگ‌های اجرا در ریپوی عمومی برای همه دیدنی است. کلید هرگز نباید ورودی ورک‌فلو باشد.

## فرمت رمزنگاری (S3D1)

توضیح کامل در سربرگ `scripts/s3d.py`. خلاصه: هدر ۱۶ بایتی (`S3D1` + پیشوند nonce هشت‌بایتی + اندازه‌ی chunk)، سپس chunk‌های ۴ مگابایتی AES-GCM. nonce هر chunk برابر پیشوند به‌اضافه‌ی شماره‌ی chunk است و AAD شامل شماره‌ی chunk و پرچم «آخرین chunk» است. این فرمت با WebCrypto مرورگر سازگار است.

## تست دستی

```bash
export S3D_KEY_B64=$(python scripts/s3d.py keygen)
JOB=$(openssl rand -hex 8)
python scripts/s3d.py encrypt-dir ./photos ./enc
gh release create in-$JOB ./enc/*.enc --repo samtqorve-lab/samat-3d --title in-$JOB --notes test
gh secret set TEST_KEY_B64 --body "$S3D_KEY_B64" --repo samtqorve-lab/samat-3d
gh workflow run build-3d.yml --repo samtqorve-lab/samat-3d -f job_id=$JOB
```

برای دیدن مدل: `model.glb.enc` را از Release `model-$JOB` بگیرید و با `python scripts/s3d.py decrypt model.glb.enc model.glb` باز کنید.

## نکات

- عکس‌ها باید **EXIF/GPS** داشته باشند. ODM از آن برای مقیاس و سرعت تطبیق استفاده می‌کند. اگر اپ عکس را کوچک می‌کند، باید EXIF را حفظ کند.
- تنظیمات پیش‌فرض ODM سبک هستند (کیفیت پایین، بدون ارتوموزاییک). با ورودی `odm_options` قابل تغییرند.
- Failed jobها عکس‌ها را نگه می‌دارند تا بشود دوباره اجرا کرد، و `cleanup-stale-photos` بعد از ۲ روز آن‌ها را پاک می‌کند.
- اگر دانلود مستقیم Release از مرورگر به‌خاطر CORS مسدود شد، دانلود مدل از طریق Edge Function پراکسی می‌شود.
