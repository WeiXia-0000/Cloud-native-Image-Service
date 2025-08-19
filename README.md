# Cloud-native Image Service

A serverless image processing and delivery service built on AWS, demonstrating different caching and performance optimization strategies.

## Architecture Overview

This project implements three progressively optimized variants of a cloud-native image service:

### 🚀 Baseline
- **API Gateway** → **Lambda** → **DynamoDB** (metadata) + **S3 presigned URLs** (images)
- Direct S3 presigned URL generation for image delivery
- Simple, no-cache architecture for baseline performance measurement

### ⚡ Better  
- **Baseline** + **CloudFront CDN** for global image delivery
- CloudFront serves processed images directly from S3
- Reduced latency through edge caching and optimized delivery paths

### 🔥 Redis
- **Better** + **Redis cache** for metadata and thumbnail lookups
- Redis caching layer reduces DynamoDB read latency
- Optimized for high-throughput metadata operations

## Technical Features

### Serverless Infrastructure
- **AWS Lambda** for compute (Python 3.11)
- **API Gateway** for REST API endpoints
- **DynamoDB** for metadata storage
- **S3** for source and processed image storage
- **CloudFront** for global content delivery
- **CloudFormation/SAM** for infrastructure as code

### Performance Optimizations
- **Redis caching** with configurable TTL and negative caching
- **CloudFront edge caching** with optimized cache policies
- **Lambda cold start mitigation** through connection pooling
- **Cross-region Redis** with timeout and retry strategies
- **Presigned URL generation** for secure S3 access

### API Endpoints
- `GET /health` - Service health check
- `GET /meta/{image}` - Retrieve image metadata
- `GET /img/{image}` - Get image delivery URL (redirect or direct)
- `HEAD /meta/{image}` - Check metadata existence
- `HEAD /img/{image}` - Check image availability

## Performance Characteristics

### Expected vs Actual Results

**Expected Performance:**
| Variant | Expected META | Expected IMG | Cache Strategy |
|---------|---------------|--------------|----------------|
| **Baseline** | ~50-100ms | ~20-50ms | None |
| **Better** | ~50-100ms | ~5-20ms | CloudFront only |
| **Redis** | ~10-50ms | ~5-20ms | Redis + CloudFront |

**Actual Results** (200 requests, 20 concurrency):
| Variant | META Latency (p50/p95/p99) | IMG Latency (p50/p95/p99) | Cache Strategy |
|---------|---------------------------|---------------------------|----------------|
| **Baseline** | 46ms / 231ms / 2014ms | 234ms / 281ms / 308ms | None |
| **Better** | 34ms / 262ms / 1802ms | 207ms / 258ms / 266ms | CloudFront only |
| **Redis** | 102ms / 259ms / 1776ms | 203ms / 245ms / 265ms | Redis + CloudFront |

### Performance Analysis

**✅ What Matches Expectations:**
- **Baseline META** (46ms p50): Close to expected 50-100ms range
- **Better META** (34ms p50): Excellent performance, better than expected
- **IMG consistency**: All variants show similar IMG performance (~200-230ms p50)

**❌ What Differs from Expectations:**

1. **IMG Latency Higher Than Expected**
   - **Expected**: 5-20ms for Better/Redis, 20-50ms for Baseline
   - **Actual**: ~200-230ms across all variants
   - **Reason**: Current benchmark tests direct CloudFront/S3 access, but CloudFront isn't showing expected performance benefits
   - **Potential Issues**:
     - CloudFront distribution may not be fully deployed/warmed up
     - Cache policies might not be optimized for the test scenario
     - Network path to CloudFront edge locations may not be optimal

2. **Redis META Performance Worse Than Expected**
   - **Expected**: 10-50ms (faster than Baseline)
   - **Actual**: 102ms p50 (slower than Baseline's 46ms)
   - **Reasons**:
     - Cross-region Redis (Upstash) adds ~50-100ms network latency
     - Low concurrency (20) doesn't benefit from Redis connection pooling
     - Redis timeout/retry overhead in public network environment

3. **Better Outperforms Redis in META**
   - **Better**: 34ms p50 (best performance)
   - **Redis**: 102ms p50 (worst performance)
   - **Reason**: Direct DynamoDB access is faster than cross-region Redis for low-concurrency workloads

**🔍 Key Insights:**
- **CloudFront benefits** are realized when accessing images directly, not through API Gateway
- **Redis overhead** dominates in cross-region, low-concurrency scenarios
- **Better variant** provides the best balance of simplicity and performance
- **High concurrency** would likely show Redis benefits more clearly

**🚀 Suggested Improvements:**
- **CloudFront optimization**: Review cache policies and ensure proper warm-up
- **Benchmark enhancement**: Add separate tests for API Gateway vs direct access
- **Redis placement**: Consider same-region Redis for better latency
- **Concurrency testing**: Test with higher concurrency to see Redis benefits

## Quick Start

### Prerequisites
- AWS CLI, SAM CLI, curl, jq
- AWS credentials configured

### Deploy & Test
```bash
# Deploy one variant
./deploy.sh baseline    # or better, redis
source .stack_env

# Upload test image
aws s3 cp sample.jpg "s3://$SRC_BUCKET/sample.jpg"
sleep 5

# Run benchmarks
./Benchmark.sh baseline  # or better, redis
```

### Smoke Tests
```bash
# Health check
curl -s "$API_URL/health"

# Get metadata
curl -s "$API_URL/meta/sample.jpg" | jq

# Get image URL
curl -i "$API_URL/img/sample.jpg"
```

## Design Decisions

### Why Three Variants?
- **Baseline**: Establishes performance baseline without caching
- **Better**: Demonstrates CDN benefits for global image delivery  
- **Redis**: Shows how caching can optimize metadata-heavy workloads

### Redis Configuration
- **Cross-region setup** with Upstash for realistic latency testing
- **Configurable timeouts** to handle network variability
- **Negative caching** to reduce repeated 404 lookups
- **Connection pooling** to minimize cold start overhead

### CloudFront Integration
- **Origin Access Control** for secure S3 access
- **Cache policies** optimized for image delivery
- **Edge locations** for global performance

## Performance Benchmarking

The included `Benchmark.sh` script measures:
- **Latency percentiles** (p50, p95, p99)
- **Requests per second** (RPS)
- **Cache hit rates** for CloudFront
- **Cold vs warm performance**

## Troubleshooting

### Common Issues
- **"No changes to deploy"**: Normal for unchanged stacks
- **CloudFront 0% hits**: Warm up cache with repeated requests
- **Redis timeouts**: Adjust `REDIS_TIMEOUT_MS` environment variable
- **Stack rollback**: Delete and redeploy with `aws cloudformation delete-stack`

### Environment Variables
- `API_URL`: API Gateway endpoint
- `SRC_BUCKET`: Source image bucket
- `DST_BUCKET`: Processed image bucket  
- `CF_DOMAIN`: CloudFront distribution domain
- `REDIS_TIMEOUT_MS`: Redis connection timeout (default: 300ms)

## Contributing

This project demonstrates cloud-native patterns for:
- Serverless image processing
- Multi-tier caching strategies
- Performance optimization techniques
- Infrastructure as code with SAM

Feel free to experiment with different configurations and optimizations!