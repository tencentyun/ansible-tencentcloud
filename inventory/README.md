# TencentCloud CVM inventory script

If you use TencentCloud CVM, maintaining an inventory file might not be the best approach, because hosts may come and go over time, be managed by external applications, or you might even be using autoscaling. For this reason, you can use the [CVM inventory script](https://github.com/tencentyun/ansible-tencentcloud/inventory/tencent_cloud.py).

## Install

1. Firstly, this depends on [TencentCloud SDK](https://github.com/TencentCloud/tencentcloud-sdk-python), you need to install it. 

```
pip install tencentcloud-sdk-python
```

2. Install TencentCloud CVM inventory script.

```
git clone https://github.com/tencentyun/ansible-tencentcloud
cd inventory
chmod +x tencent_cloud.py
```

3. To make a successful API call to TencentCloud, you must configure the credential, the simplest way is to export two environment variables:

```
export TENCENTCLOUD_SECRET_ID=Test1234
export TENCENTCLOUD_SECRET_KEY=Test1234
```

4. You can test the TencentCloud dynamic inventory script manually to confirm it is working as expected:

```
./tencent_cloud.py --list
```

## Usage

### Configuration

There are other config options in tencent_cloud.ini, including cache control and destination variables. By default, the tencent_cloud.ini file is configured for all regions, but you can comment out any features that aren’t applicable.

If you are running Ansible from within CVM in same vpc, internal IP addresses may make more sense than public IP addresses. In this case, you can modify the destination_variable in tencent_cloud.ini to be the private IP addresses of an instance. 

### Cache

The TencentCloud dynamic inventory script will cache results to avoid repeated API calls. To explicitly clear the cache, you can run the tencent_cloud.py script with the –refresh argument:

```
./tencent_cloud.py --refresh-cache
```

### Test

Once you confirm the dynamic inventory script is working as expected, you can tell Ansible to use the tencent_cloud.py script as an inventory file, as illustrated below:

```
ansible -i tencent_cloud.py all -m ping -k -u root
```
