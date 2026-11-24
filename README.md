# SaveLivo 0.3.1 – Instagram ve TikTok özel test modu

Bu paket SaveLivo 0.3'ün üzerine uygulanır. Eski çalışan MP4/PNG testleri korunur.

## Yeni özellik

Instagram post/reel ve TikTok tekil video URL'lerini deneysel olarak hazırlama denemeleri.
Yalnızca herkese açık, indirilmesine izin verilmiş içerikler denenir. Özel hesaplar, DRM,
çerez/giriş, hikâyeler ve YouTube indirme desteği BU TESTTE YOKTUR.

Bu motorun platformlarda gerçekten indirme yapabildiği canlı olarak doğrulanmadı.
Özellikle Instagram kimlik doğrulama gerektirebilir.

## Kurulum (önce backend)

1. ZIP içindeki `main.py`, `media_jobs.py`, `render.yaml`, `requirements.txt`,
   `ornek.mp4`, `ornek-fotograf.png` ve `README.md` dosyalarını GitHub'daki
   `furkandikmen38/savelivo-api` deposuna yükleyin.
2. Render dashboard > savelivo-api > Deploys > Manual Deploy > Deploy latest commit.
3. `/api/health` cevabında `surum: 0.3.1` görünmesini doğrulayın.
4. Render'da Environment'a şu iki değişkeni ekleyin:
   - `ENABLE_PUBLIC_EXTRACTORS` = `true`
   - `SAVELIVO_TEST_KEY` = EN AZ 24 RASTGELE KARAKTERLIK KENDINIZE AIT SIR (GitHub'a yazmayın!)
   Kaydet ve yeniden dağıt (Save and deploy). Anahtarı sohbet mesajında veya ekran görüntüsünde paylaşmayın.
5. Netlify'da `SaveLivo-v031-Netlify-Test.zip` dosyasını mevcut sitenin Deploys alanına yükleyin.
6. Web sitesinde `Instagram / TikTok özel test modu` bölümünü açın, Render'da seçtiğiniz
   anahtarı yalnızca kendi tarayıcınızda girin; izin verilen kendi tekil gönderinizi deneyin.

## İndirme davranışı

- Video indirme alanları korunur.
- Fotoğraf bağlantılarında tek fotoğraf indirilir.
- Carousel/albüm bağlantılarında yalnızca ilk fotoğraf sunulur.
- Birden fazla dosya, ZIP veya toplu indirme üretilmez.

## Notlar

- `ENABLE_PUBLIC_EXTRACTORS` = false veya test anahtarı yok/24 karakterden kısa ise
  sosyal medya motoru açılmaz. Sağlık kontrolü açık kalır.
- Anahtar sunucuya yalnızca HTTPS `X-SaveLivo-Test-Key` başlığında iletilir; kalıcı
  tarayıcı depolamasına yazılmaz. Dosya indirme de anahtar gerektirir.
- Bu bir kontrollü pilot testtir; ayrı staging sunucusu ve ağ seviyesinde SSRF/egress
  izolasyonu, kullanıcı kotası ve kalıcı depolama olmadan herkese açık hizmet yapılmamalı.
- Render Free uykuya geçebilir, sonuç dosyası sunucu yeniden başlatılınca silinir.
- Platformların koşullarını, hak sahipliğini ve indirme izinlerini değerlendirin.

## Kontrol adresleri

- https://savelivo-api.onrender.com/api/health
- https://savelivo-api.onrender.com/api/medya-yetenekleri
- https://super-selkie-f65d2d.netlify.app/
