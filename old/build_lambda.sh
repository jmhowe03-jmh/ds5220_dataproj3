#!/usr/bin/env bash
# build_lambda.sh
# Packages lambda_function.py + all dependencies into lambda.zip
# for upload to AWS Lambda.
#
# Usage:
#   chmod +x build_lambda.sh
#   ./build_lambda.sh
#
# Output: lambda.zip  (upload this file to your Lambda function in the AWS console)

set -euo pipefail

PACKAGE_DIR="package"
ZIP_NAME="lambda.zip"

echo "==> Cleaning previous build..."
rm -rf "$PACKAGE_DIR" "$ZIP_NAME"

echo "==> Installing dependencies into ./$PACKAGE_DIR/ ..."
pip install \
  --isolated \
  --platform manylinux2014_x86_64 \
  --target "$PACKAGE_DIR" \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  openmeteo-requests \
  requests-cache \
  retry-requests \
  boto3 \
  pandas \
  matplotlib \
  seaborn

echo "==> Copying lambda_function.py into package..."
cp lambda_function.py "$PACKAGE_DIR/"

echo "==> Creating $ZIP_NAME ..."
cd "$PACKAGE_DIR"
zip -r9 "../$ZIP_NAME" . -x "*.pyc" -x "*/__pycache__/*"
cd ..

echo ""
echo "✅  Done!  Upload $(du -sh $ZIP_NAME | cut -f1) $ZIP_NAME to your Lambda function."
echo "   In the AWS console: Lambda → your function → Code → Upload from → .zip file"
