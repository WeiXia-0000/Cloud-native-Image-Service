# Cloud-native Image Service
A distributed, serverless image processing and delivery pipeline built with AWS services.  
Automatically processes uploaded images (resize, format conversion) and delivers them through a cache-first read path for low latency and scalability.

---

## 1. Introduction
This service is designed to:
- Handle **image uploads** from clients (web/mobile).
- Automatically **resize and convert formats** on upload.
- **Store processed images** in distributed object storage.
- Provide **low-latency reads** through a cache-first design.
- Scale horizontally with traffic while ensuring high availability.

---

## 2. Real-world Use Cases
This architecture matches common production needs in:
- **E-commerce** (product thumbnails, multi-resolution images)
- **Social media** (user-uploaded media optimization)
- **News/media sites** (responsive images for different devices)
- **Maps & ads** (quick delivery of frequently accessed assets)

**Example:**  
> In an e-commerce platform, product images are uploaded once, automatically processed into multiple sizes, and served through Redis/CloudFront to handle millions of requests with sub-millisecond latency for hot items.

---

## 3. High-level Architecture
![Architecture Diagram](cloud_native_image_service_distributed_view.png)

---

## 4. Distributed Systems Design Highlights
- **Distributed storage:** Amazon **S3** + **DynamoDB** with multi-AZ replication for durability.
- **Distributed caching:** **ElastiCache (Redis cluster mode)** with sharding + replication for high availability.
- **Event-driven parallelism:** S3 triggers Lambda functions for concurrent image processing.
- **Consistency:** Cache invalidation after processing to avoid stale data.
- **Fault tolerance:** Multi-AZ deployment, automated retries, replicated cache nodes.
- **Scalability:** Serverless functions auto-scale with load; global delivery via CloudFront.
- **Performance:** Cache-first read path reduced median latency by ~60% compared to direct S3/DynamoDB reads.

---

## 5. Data Flow
### Write Path
1. Client uploads image to **S3 Source Bucket** (direct or via API Gateway).
2. S3 triggers **Lambda (Processor)**.
3. Lambda resizes and converts the image with **Pillow**.
4. Processed image stored in **S3 Processed Bucket**.
5. Metadata written to **DynamoDB** (and optionally cached in Redis).

### Read Path
1. Client requests an image via **API Gateway → Lambda (Reader)**.
2. Lambda checks **Redis** for metadata.
3. **Cache hit:** Returns a **302 redirect** to CloudFront/S3.
4. **Cache miss:** Reads DynamoDB, fills Redis, returns redirect.

---

## 6. Quick Start
### Prerequisites
- AWS account & CLI configured
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html)
- Python 3.11+
- (Optional) AWS ElastiCache Redis & CloudFront distribution

### Deployment
```bash
sam build
sam deploy --guided
