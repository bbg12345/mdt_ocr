MDT OCR 服务 — 调用说明（人读版）
====================================

本仓库提供三个 HTTP 接口：身份证识别、机动车行驶证识别、车险保单 PDF 字段抽取。身份证与行驶证
由服务端配置腾讯云密钥，调用方不需要也不应在请求里带 SecretId/SecretKey。车险保单接口在服务端
解析 PDF（pypdf / PyMuPDF 等），不经过腾讯云 OCR；可选的承保公司豆包兜底需单独配置环境变量
（见第五节）。

身份证与行驶证：请求体为 JSON，图片通过 image_url 传入（公网可访问的图片地址）。
其它传参方式（例如 Base64、上传文件）以运行中的服务及 /docs 为准，本文不展开。

车险保单：请求体为 JSON，须传 PDF 地址 pdf_url（公网 http(s) URL，或服务进程可读的本机绝对路径）。

约定：调用时 card_side 必传。身份证为 FRONT 或 BACK；机动车行驶证为 FRONT、BACK 或 DOUBLE。
不传或传空时，请勿依赖本文档描述的行为，应以接口实际校验与返回为准。

默认监听示例 http://主机名:8080。浏览器打开 http://主机名:8080/docs 可见 Swagger。GET /health
可查看当前三个接口的路径（id_card_ocr、driver_license_ocr、car_insurance_extract）及线程池配置。


一、身份证 OCR
--------------------------------

POST，默认路径 /api/v1/ocr/idcard（若服务端设置了 OCR_IDCARD_PATH，以环境变量为准）。

Content-Type 使用 application/json。

请求体须包含 image_url（图片 URL）与 card_side（必传）。身份证：FRONT 为人像面，BACK 为国徽面。
可选字段 config（腾讯云 Config 的 JSON 字符串），一般可省略。

成功时返回 200，正文为 JSON。因 card_side 必传，本服务会按面别裁剪：FRONT 时只含 Name、IdNum、
Sex、Address；BACK 时只含 ValidDate。

参数错误、缺字段等常见为 400；腾讯云失败常见为 502，正文里常有 code、message、requestId。


二、车险保单 PDF 字段抽取
--------------------------------

从车险保单 PDF 中抽取结构化字段（交强险 / 商业险），不是图片 OCR。POST，默认路径
/api/v1/ocr/car-insurance-extract（若服务端设置了 OCR_CAR_INSURANCE_PATH，以环境变量为准）。

Content-Type 使用 application/json。

请求体须包含：
  type    字符串，保单类型：compulsory（交强险）或 commercial（商业险）
  pdf_url 字符串，PDF 的 http(s) 地址，或服务所在机器可读的本地绝对路径

成功时返回 200，正文为 JSON。常见顶层字段包括 kv（键值对，未识别的标量字段多为空字符串 ""）、
known_insurance_company（命中已知承保枚举时的内部名称，未命中为 ""）；商业险在部分承保方下会
合并「保险费明细」相关保额/保费等字段。具体键名与合并规则以实现为准，可对照 Swagger 或服务端
日志（各 pass 选用与兜底逻辑见 car_insurance 模块）。

常见错误：参数缺失或 type 取值非法多为 400；PDF 无法读取多为 400，本地路径不存在可为 404；
抽取过程异常多为 502，正文含 detail。

curl 示例（将服务地址与 pdf_url 换成实际值；商业险 type 为 commercial，交强险为 compulsory）：

    curl -sS -X POST "http://127.0.0.1:8080/api/v1/ocr/car-insurance-extract" \
      -H "Content-Type: application/json; charset=utf-8" \
      -d '{"type":"compulsory","pdf_url":"https://example.com/policy.pdf"}'


三、机动车行驶证 OCR
----------------------------------------------

这是机动车行驶证，不是驾驶证。POST，默认路径 /api/v1/ocr/driver-license（可用
OCR_DRIVER_LICENSE_PATH 覆盖）。

同样只用 application/json，须包含 image_url 与 card_side（必传）。行驶证：FRONT 主页、BACK 副页、
DOUBLE 正副页同框。

成功时 200，JSON 正文。在 card_side 必传的前提下：FRONT 时返回裁剪后的 PlateNo、Vin、EngineNo、
Owner、Model、RegisterDate；BACK 时仅 PlateNo；DOUBLE 时含 FrontInfo（上述六字段）与 BackInfo（仅 PlateNo）。

错误情况与上一节类似。


四、Java 调用示例（身份证/行驶证须传 card_side；保单须传 type + pdf_url，JDK 11 及以上）
----------------------------------------------------------

使用 Java 11 的 HttpClient，POST JSON。请将 BASE_URL 换成你的服务根地址；身份证/行驶证将图片 URL
换成真实可访问地址；保单将 pdf_url 换成公网 PDF 或服务端可读路径。

// ---------------------------------------------------------------------------
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

public class OcrClientExample {

    private static final String BASE_URL = "http://127.0.0.1:8080";
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    /** 身份证人像面：须同时传 image_url 与 card_side=FRONT */
    public static String idCardFront(String imageUrl) throws Exception {
        String json = "{"
                + "\"image_url\":\"" + escapeJson(imageUrl) + "\","
                + "\"card_side\":\"FRONT\""
                + "}";
        return postJson(BASE_URL + "/api/v1/ocr/idcard", json);
    }

    /** 机动车行驶证正副同框：须同时传 image_url 与 card_side=DOUBLE */
    public static String vehicleLicenseDouble(String imageUrl) throws Exception {
        String json = "{"
                + "\"image_url\":\"" + escapeJson(imageUrl) + "\","
                + "\"card_side\":\"DOUBLE\""
                + "}";
        return postJson(BASE_URL + "/api/v1/ocr/driver-license", json);
    }

    /** 车险商业险保单：须传 type=commercial 与 pdf_url（公网 PDF 或服务器可读路径） */
    public static String carInsuranceCommercial(String pdfUrl) throws Exception {
        String json = "{"
                + "\"type\":\"commercial\","
                + "\"pdf_url\":\"" + escapeJson(pdfUrl) + "\""
                + "}";
        return postJson(BASE_URL + "/api/v1/ocr/car-insurance-extract", json);
    }

    /** 车险交强险保单：须传 type=compulsory 与 pdf_url */
    public static String carInsuranceCompulsory(String pdfUrl) throws Exception {
        String json = "{"
                + "\"type\":\"compulsory\","
                + "\"pdf_url\":\"" + escapeJson(pdfUrl) + "\""
                + "}";
        return postJson(BASE_URL + "/api/v1/ocr/car-insurance-extract", json);
    }

    private static String postJson(String url, String jsonBody) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .timeout(Duration.ofSeconds(120))
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> response = HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        int code = response.statusCode();
        if (code != 200) {
            throw new IllegalStateException("HTTP " + code + ": " + response.body());
        }
        return response.body();
    }

    private static String escapeJson(String s) {
        if (s == null) {
            return "";
        }
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
// ---------------------------------------------------------------------------

调用超时由客户端 HTTP 库自行设置，服务端没有单独的「超时配置接口」。
  Java 11 HttpClient：建连上限在 HttpClient.newBuilder().connectTimeout(...)；单次请求（发起到读完
  响应体）在 HttpRequest.Builder.timeout(...)。上文示例为建连 10 秒、整请求 120 秒。身份证/行驶证
  通常远小于 120 秒；车险保单 PDF 抽取可能较慢，若遇超时请把 HttpRequest 的 timeout 调大（项目内
  联调常用 600 秒量级，见 test/test_insurance.py）。
  curl：例如 --connect-timeout 10 --max-time 600（建连上限与整次请求总上限）。
  Python requests：requests.post(..., timeout=(10, 600))，表示连接 10 秒、读响应最多 600 秒；也可
  传单个数字表示二者共用同一上限。

若部署修改了路径，请把代码中的路径改成 GET /health 返回的 id_card_ocr、driver_license_ocr、
car_insurance_extract。
生产环境建议用 Jackson、Gson 等构造 JSON。


五、服务端部署相关（供运维参考）
------------------------------

腾讯云凭证：环境变量 TENCENTCLOUD_SECRET_ID、TENCENTCLOUD_SECRET_KEY。OCR_THREAD_POOL_SIZE 默认 30。
使用 python ocr_service.py 时可用 OCR_HOST、OCR_PORT 指定监听。

车险保单可选：承保公司等豆包兜底需 CAR_INSURANCE_DOUBAO_API_KEY、CAR_INSURANCE_DOUBAO_MODEL（方舟），
请求基址为 https://ark.cn-beijing.volces.com/api/v3；未配置时仍走规则与 PDF 解析路径。
