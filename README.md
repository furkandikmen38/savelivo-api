# SaveLivo 0.3.2 – Instagram, TikTok ve Vimeo kontrollü test modu

Bu paket SaveLivo 0.3'ün üzerine uygulanır. Eski çalışan MP4/PNG testleri korunur.

## Yeni özellik

Instagram post/reel, TikTok tekil video ve Vimeo tekil herkese açık video URL'lerini deneysel olarak hazırlama denemeleri.
Yalnızca herkese açık, indirilmesine izin verilmiş içerikler denenir. Özel hesaplar, parola korumalı Vimeo videoları, DRM,
çerez/giriş, hikâyeler ve YouTube indirme desteği BU TESTTE YOKTUR.

Bu motorun platformlarda gerçekten indirme yapabildiği canlı olarak doğrulanmadı.
Vimeo'da yalnızca herkese açık tekil video sayfaları denenir; indirme her videoda garanti değildir.
Instagram kimlik doğrulama gerektirebilir.

## Kurulum (önce backend)

1. ZIP içindeki `main.py`, `media_jobs.py`, `render.yaml`, `requirements.txt`,
   `ornek.mp4`, `ornek-fotograf.png` ve `README.md` dosyalarını GitHub'daki
   `furkandikmen38/savelivo-api` deposuna yükleyin.
2. Render dashboard > savelivo-api > Deploys > Manual Deploy > Deploy latest commit.
3. `/api/health` cevabında `surum: 0.3.2` görünmesini doğrulayın.
4. Render'da Environment'a şu iki değişkeni ekleyin:
   - `ENABLE_PUBLIC_EXTRACTORS` = `true`
   - `SAVELIVO_TEST_KEY` = EN AZ 24 RASTGELE KARAKTERLIK KENDINIZE AIT SIR (GitHub'a yazmayın!)
   Kaydet ve yeniden dağıt (Save and deploy). Anahtarı sohbet mesajında veya ekran görüntüsünde paylaşmayın.
5. Netlify'da `SaveLivo-v031-Netlify-Test.zip` dosyasını mevcut sitenin Deploys alanına yükleyin.
6. Web sitesindeki özel test alanında Render'da seçtiğiniz anahtarı yalnızca kendi tarayıcınızda kullanın.
   Vimeo için yalnızca herkese açık ve indirme hakkınız olan tekil video bağlantısını deneyin.
   Arayüzün `/api/incele` yanıtındaki `hazirlama_gerekli` durumunu ve ardından `/api/hazirla` iş akışını desteklemesi gerekir.

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
